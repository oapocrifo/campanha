"""Wiki da campanha.

python wiki.py atualizar transcricoes/sessao-01.md   envia a sessão ao Gemini e atualiza wiki/dados.json
python wiki.py paginas                                gera as páginas do site em wiki/docs
"""
import json
import os
import posixpath
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

DADOS = Path("wiki/dados.json")
DOCS = Path("wiki/docs")
# em ordem de preferência; quando um está sobrecarregado (503) ou sem cota (429), tenta o seguinte.
# O Google bloqueou os modelos 2.5 para projetos novos em set/2026.
MODELOS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
TIPOS = {  # tipo: (pasta, nome da seção, nome no singular)
    "personagem": ("personagens", "Personagens", "Personagem de jogador"),
    "npc": ("npcs", "NPCs", "NPC"),
    "criatura": ("criaturas", "Criaturas", "Criatura"),
    "local": ("locais", "Locais", "Local"),
    "faccao": ("faccoes", "Facções", "Facção"),
    "item": ("itens", "Itens", "Item"),
    "evento": ("eventos", "Eventos", "Evento"),
}

INSTRUCOES = """Você mantém a wiki de uma campanha de RPG de mesa.
Recebe o índice atual da wiki e a transcrição de uma sessão.
Cada linha da transcrição segue o formato [hh:mm:ss] Personagem (Jogador): fala.
Os personagens dos jogadores são os que aparecem como "Personagem (Jogador)". O Mestre narra e interpreta todos os NPCs.

Regras:
- Registre só o que aconteceu ou foi revelado dentro da ficção. Ignore conversa fora do jogo, regras, pausas e assuntos pessoais.
- Não invente. Se algo for incerto na transcrição, escreva que é incerto ou deixe de fora.
- A transcrição vem de reconhecimento de voz e escreve nomes de várias formas erradas. Quando um nome se parecer com um da lista GRAFIAS CORRETAS ou do índice, use a grafia correta.
- Para algo que já está no índice, use exatamente o id do índice. Para algo novo, deixe o id vazio.
- tipo: personagem (só personagens dos jogadores), npc, criatura (espécies e monstros), local, faccao, item, evento (acontecimentos marcantes com nome próprio ou importância duradoura, como batalhas, mortes e rituais).
- resumo da entidade: a descrição completa e atualizada em até 4 frases, juntando o resumo do índice com o que esta sessão revelou.
- fatos: só informações novas desta sessão, uma frase cada, sem repetir o que já está no resumo do índice.
- atributos: campos curtos ditos na sessão, como Raça, Classe, Ocupação, Status, Localização, Afiliação, Jogador. Para personagens dos jogadores, inclua Jogador.
- linha_do_tempo: os acontecimentos principais da sessão, em ordem, uma frase cada.
- pendencias_novas: missões, mistérios, ameaças, dívidas ou promessas que surgiram e ficaram sem solução.
- pendencias_resolvidas: ids das pendências abertas do índice que foram resolvidas nesta sessão, com uma frase sobre como.
- titulo: um título curto para a sessão, sem o número dela. resumo: 2 a 4 parágrafos separados por linha em branco.
- recapitulacao: até 6 frases para os jogadores lerem antes da próxima sessão: onde o grupo está, o que acabou de acontecer, quem está envolvido e quais objetivos estão em aberto. Use esta sessão e os resumos das sessões anteriores.
- Escreva em português do Brasil, com frases curtas e nomes grafados como no índice."""

TEXTO = {"type": "STRING"}
LISTA = {"type": "ARRAY", "items": TEXTO}
ESQUEMA = {
    "type": "OBJECT",
    "required": ["titulo", "resumo", "recapitulacao", "linha_do_tempo", "entidades", "pendencias_novas",
                 "pendencias_resolvidas"],
    "properties": {
        "titulo": TEXTO,
        "resumo": TEXTO,
        "recapitulacao": TEXTO,
        "linha_do_tempo": LISTA,
        "entidades": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "required": ["id", "nome", "tipo", "resumo", "fatos"],
            "properties": {
                "id": TEXTO, "nome": TEXTO, "tipo": {"type": "STRING", "enum": list(TIPOS)},
                "apelidos": LISTA, "resumo": TEXTO, "fatos": LISTA,
                "atributos": {"type": "ARRAY", "items": {
                    "type": "OBJECT", "required": ["campo", "valor"],
                    "properties": {"campo": TEXTO, "valor": TEXTO}}},
            }}},
        "pendencias_novas": LISTA,
        "pendencias_resolvidas": {"type": "ARRAY", "items": {
            "type": "OBJECT", "required": ["id", "como"], "properties": {"id": TEXTO, "como": TEXTO}}},
    },
}


class Pausa(Exception):
    """Limite do serviço atingido. O trabalho continua na próxima execução."""


def carregar():
    if DADOS.exists():
        return corrigir(reparar(json.loads(DADOS.read_text(encoding="utf-8"))))
    return {"sessoes": {}, "entidades": {}, "pendencias": []}


def reparar(dados):
    """Desfaz acentos corrompidos ("LeÃ£o" vira "Leão"), gravados pela versão de 26/10 da leitura do Gemini.
    Páginas cujo nome estava corrompido ganham o endereço do nome certo."""
    def texto(t):
        try:
            return t.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):  # texto já correto
            return t

    def andar(x):
        if isinstance(x, str):
            return texto(x)
        if isinstance(x, list):
            return [andar(v) for v in x]
        if isinstance(x, dict):
            return {texto(k): andar(v) for k, v in x.items()}
        return x

    novo, trocas = andar(dados), {}
    for i, e in list(novo["entidades"].items()):
        if e["nome"] != dados["entidades"][i]["nome"]:
            base = j = f"{TIPOS.get(e['tipo'], TIPOS['npc'])[0]}/{slug(e['nome'])}"
            n = 2
            while j != i and j in novo["entidades"]:
                j, n = f"{base}-{n}", n + 1
            if j != i:
                novo["entidades"][j] = novo["entidades"].pop(i)
                trocas[i] = j
    for s in novo["sessoes"].values():
        s["entidades"] = [trocas.get(i, i) for i in s["entidades"]]
        s["titulo"] = re.sub(r"^sess[aã]o\s*\d+\s*[-–:]\s*", "", s["titulo"], flags=re.I)  # o número já aparece antes
    return novo


def corrigir(dados):
    """wiki/correcoes.txt, uma linha por correção:
    "Nome errado = Nome certo" corrige a grafia ou junta duas páginas da mesma coisa.
    "Nome errado =" (sem nada depois) apaga a página."""
    arquivo = Path("wiki/correcoes.txt")
    if not arquivo.exists():
        return dados
    ents = dados["entidades"]
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if "=" not in linha or linha.strip().startswith("#"):
            continue
        errado, certo = (x.strip() for x in linha.split("=", 1))
        a = next((i for i, e in ents.items() if errado.lower() in (n.lower() for n in [e["nome"], *e["apelidos"]])), None)
        if a is None:
            continue
        b = next((i for i, e in ents.items() if e["nome"].lower() == certo.lower()), None)
        if not certo or (b and b != a):
            x = ents.pop(a)
            if certo:  # junta a no b
                y = ents[b]
                y["apelidos"] = sorted({*y["apelidos"], x["nome"], *x["apelidos"]} - {y["nome"]})
                y["fatos"] = x["fatos"] + y["fatos"]
                y["sessoes"] = sorted({*y["sessoes"], *x["sessoes"]}, key=ordem)
                y["atributos"] = x["atributos"] | y["atributos"]
            for s in dados["sessoes"].values():
                s["entidades"] = list(dict.fromkeys(b if i == a else i for i in s["entidades"] if i != a or certo))
        else:  # só a grafia
            e = ents[a]
            e["apelidos"] = sorted({*e["apelidos"], e["nome"]} - {certo})
            e["nome"] = certo
    return dados


def ordem(ep):
    """sessao-2 vem antes de sessao-10."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ep.lower())]


def slug(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", texto).strip("-") or "sem-nome"


# ---------- atualização com o Gemini ----------

def gemini(texto, instrucoes=INSTRUCOES, esquema=ESQUEMA):
    import socket

    import requests  # só a atualização precisa; a geração de páginas roda sem dependências
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection

    class ComKeepalive(HTTPAdapter):
        """Manda sinais na conexão parada: a rede do GitHub derruba conexões sem tráfego por alguns minutos."""
        def init_poolmanager(self, *a, **k):
            k["socket_options"] = HTTPConnection.default_socket_options + [
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)]
            super().init_poolmanager(*a, **k)

    sessao = requests.Session()
    sessao.mount("https://", ComKeepalive())
    corpo = {
        "systemInstruction": {"parts": [{"text": instrucoes}]},
        "contents": [{"role": "user", "parts": [{"text": texto}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": esquema,
                             "maxOutputTokens": 65536},
        "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in (
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],  # combates e vilões de RPG
    }
    for tentativa in range(3 * len(MODELOS)):
        modelo = MODELOS[tentativa % len(MODELOS)]
        # streaming: a resposta chega aos pedaços enquanto o Gemini escreve, sem esperar tudo pronto
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:streamGenerateContent?alt=sse"
        inicio = time.time()
        try:
            r = sessao.post(url, json=corpo, stream=True, timeout=(30, 600),
                            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            if r.status_code == 200:
                r.encoding = "utf-8"  # o Google não informa a codificação, e sem isso os acentos se corrompem
                partes, motivo = [], None
                for linha in r.iter_lines(decode_unicode=True):
                    if linha and linha.startswith("data:"):
                        c = (json.loads(linha[5:]).get("candidates") or [{}])[0]
                        partes += [p.get("text", "") for p in c.get("content", {}).get("parts", [])
                                   if not p.get("thought")]
                        motivo = c.get("finishReason") or motivo
                print(f"Gemini ({modelo}) respondeu em {time.time() - inicio:.0f} s", flush=True)
                try:
                    return json.loads("".join(partes))
                except json.JSONDecodeError:
                    raise RuntimeError(f"Resposta do Gemini incompleta ({motivo})")
        except requests.RequestException as erro:
            print(f"Falha de conexão com o Gemini ({modelo}) após {time.time() - inicio:.0f} s ({erro})", flush=True)
            time.sleep(30)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            try:
                mensagem = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                mensagem = r.text[:200]
            print(f"Gemini ({modelo}) respondeu {r.status_code} após {time.time() - inicio:.0f} s: {mensagem}",
                  flush=True)
            time.sleep(30)
            continue
        raise RuntimeError(f"Gemini recusou o envio ({r.status_code}): {r.text[:300]}")
    raise Pausa("Gemini sem cota ou indisponível")


def mesclar(dados, ep, r):
    """Junta a resposta do Gemini aos dados. Fatos antigos nunca são apagados."""
    ents = dados["entidades"]
    por_nome = {n.lower(): i for i, e in ents.items() for n in [e["nome"], *e["apelidos"]]}
    citadas = []
    for x in r["entidades"]:
        nome = x["nome"].strip()
        i = x["id"] if x["id"] in ents else por_nome.get(nome.lower())
        if i is None:
            base = i = f"{TIPOS.get(x['tipo'], TIPOS['npc'])[0]}/{slug(nome)}"
            n = 2
            while i in ents:
                i, n = f"{base}-{n}", n + 1
            ents[i] = {"tipo": x["tipo"] if x["tipo"] in TIPOS else "npc", "nome": nome, "apelidos": [],
                       "resumo": "", "atributos": {}, "fatos": [], "sessoes": []}
        e = ents[i]
        e["resumo"] = x["resumo"].strip() or e["resumo"]
        novos = {a.strip() for a in x.get("apelidos", []) + [nome] if a.strip()}
        e["apelidos"] = sorted({*e["apelidos"], *novos} - {e["nome"]})
        e["atributos"].update({a["campo"].strip(): a["valor"].strip()
                               for a in x.get("atributos", []) if a["campo"].strip() and a["valor"].strip()})
        e["fatos"] += [[ep, f.strip()] for f in x["fatos"] if f.strip()]
        if ep not in e["sessoes"]:
            e["sessoes"].append(ep)
        for n in [e["nome"], *e["apelidos"]]:
            por_nome[n.lower()] = i
        citadas.append(i)

    abertas = {p["id"]: p for p in dados["pendencias"] if not p["resolvida_em"]}
    for x in r["pendencias_resolvidas"]:
        if x["id"] in abertas:
            abertas[x["id"]].update(resolvida_em=ep, como=x["como"].strip())
    for texto in r["pendencias_novas"]:
        if texto.strip():
            dados["pendencias"].append({"id": f"p{len(dados['pendencias']) + 1}", "texto": texto.strip(),
                                        "aberta_em": ep, "resolvida_em": None, "como": ""})
    dados["sessoes"][ep] = {"titulo": r["titulo"].strip(), "resumo": r["resumo"].strip(),
                            "linha_do_tempo": [t.strip() for t in r["linha_do_tempo"] if t.strip()],
                            "entidades": list(dict.fromkeys(citadas))}
    if ep == max(dados["sessoes"], key=ordem) and r.get("recapitulacao", "").strip():
        dados["recapitulacao"] = {"apos": ep, "texto": r["recapitulacao"].strip()}


def atualizar(transcricao):
    dados = carregar()
    ep = Path(transcricao).stem
    if ep in dados["sessoes"]:
        print(f"{ep} já está na wiki", flush=True)
        return
    indice = {
        "entidades": [{"id": i, "tipo": e["tipo"], "nome": e["nome"], "apelidos": e["apelidos"],
                       "resumo": e["resumo"][:300]} for i, e in dados["entidades"].items()],
        "pendencias_abertas": [{"id": p["id"], "texto": p["texto"]}
                               for p in dados["pendencias"] if not p["resolvida_em"]],
    }
    anteriores = [f"{e}: {dados['sessoes'][e]['resumo']}"
                  for e in sorted((e for e in dados["sessoes"] if ordem(e) < ordem(ep)), key=ordem)[-2:]]
    glossario = Path("glossario.txt").read_text(encoding="utf-8") if Path("glossario.txt").exists() else ""
    print(f"Enviando {ep} ao Gemini", flush=True)
    resposta = gemini(f"GRAFIAS CORRETAS DE NOMES DA CAMPANHA:\n{glossario.strip() or 'nenhuma'}\n\n"
                      f"ÍNDICE ATUAL DA WIKI:\n{json.dumps(indice, ensure_ascii=False)}\n\n"
                      f"RESUMOS DAS SESSÕES ANTERIORES:\n{chr(10).join(anteriores) or 'nenhuma'}\n\n"
                      f"TRANSCRIÇÃO DA SESSÃO {ep}:\n{Path(transcricao).read_text(encoding='utf-8')}")
    mesclar(dados, ep, resposta)
    DADOS.parent.mkdir(parents=True, exist_ok=True)
    DADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{ep}: {len(resposta['entidades'])} nomes, {len(resposta['pendencias_novas'])} pendências novas",
          flush=True)


# ---------- geração das páginas ----------

ESTILO = """
.md-typeset .infobox { float: right; width: 16rem; margin: 0 0 1em 1.5em; padding: .5em .7em;
  border: 1px solid var(--md-default-fg-color--lightest); border-radius: 4px;
  background: var(--md-code-bg-color); font-size: .68rem; }
.md-typeset .infobox-titulo { margin: 0 0 .3em; text-align: center; font-size: .9rem; font-weight: 700; }
.md-typeset .infobox-tipo { margin: 0 0 .5em; text-align: center; color: var(--md-default-fg-color--light); }
.md-typeset .infobox table:not([class]) { display: table; width: 100%; margin: 0; border: 0; font-size: inherit; background: transparent; }
.md-typeset .infobox thead { display: none; }
.md-typeset .infobox td { padding: .35em .4em; vertical-align: top; }
.md-typeset .infobox td:first-child { font-weight: 600; }
.md-typeset .infobox .md-typeset__scrollwrap, .md-typeset .infobox .md-typeset__table { display: block; margin: 0; padding: 0; }
.md-typeset .secoes { display: grid; grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); gap: .7rem; margin: 1em 0 2em; }
.md-typeset .secao { display: block; padding: .8em 1em; border: 1px solid var(--md-default-fg-color--lightest);
  border-radius: 6px; color: var(--md-default-fg-color); }
.md-typeset .secao:hover { border-color: var(--md-accent-fg-color); color: var(--md-default-fg-color); }
.md-typeset .secao strong { display: block; font-size: .85rem; }
.md-typeset .secao span { font-size: .7rem; color: var(--md-default-fg-color--light); }
@media (max-width: 44.9375em) { .md-typeset .infobox { float: none; width: auto; margin: 0 0 1em; } }
.md-typeset .recap { margin: 1em 0 1.5em; padding: .8em 1.1em; border-left: 4px solid var(--md-accent-fg-color);
  background: var(--md-code-bg-color); border-radius: 4px; }
.md-typeset .recap p { margin: .4em 0; }
#assistente .conta { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-bottom: 1rem; font-size: .7rem; }
#assistente select, #assistente textarea { font: inherit; color: var(--md-default-fg-color);
  background: var(--md-default-bg-color); border: 1px solid var(--md-default-fg-color--lighter); border-radius: 4px; padding: .4em; }
#assistente select { max-width: 100%; }
#assistente button { font: inherit; font-size: .7rem; cursor: pointer; padding: .45em .9em; border-radius: 4px;
  border: 1px solid var(--md-accent-fg-color); background: transparent; color: var(--md-default-fg-color); }
#assistente button.principal { background: var(--md-accent-fg-color); color: var(--md-accent-bg-color); }
#assistente button:disabled { opacity: .5; cursor: default; }
#assistente .mensagens { display: flex; flex-direction: column; gap: .8rem; margin-bottom: 1rem; }
#assistente .msg { padding: .6em 1em; border-radius: 6px; font-size: .75rem; }
#assistente .msg p { margin: .4em 0; }
#assistente .usuario { align-self: flex-end; max-width: 85%; background: var(--md-code-bg-color); }
#assistente .ia { border: 1px solid var(--md-default-fg-color--lightest); }
#assistente .erro { border: 1px solid #c62828; }
#assistente .envio { display: flex; gap: .5rem; align-items: flex-end; }
#assistente textarea { flex: 1; min-height: 3.2em; resize: vertical; }
#assistente .aviso { font-size: .65rem; color: var(--md-default-fg-color--light); }
"""


def nome_sessao(ep):
    m = re.fullmatch(r"sess[aã]o[-_ ]?0*(\d+)", ep, re.I)
    return f"Sessão {m[1]}" if m else ep


def paginas_de(n):
    return f"{n} página" if n == 1 else f"{n} páginas"


SEM_MENU = "---\nhide:\n  - navigation\n---\n\n"  # páginas fora do menu usam a largura toda


def seguro(texto):
    return texto.replace("<", "&lt;")


def paginas():
    dados = carregar()
    ents, sessoes, pends = dados["entidades"], dados["sessoes"], dados["pendencias"]
    lista = sorted(sessoes, key=ordem)  # ordem das sessões, mesmo que alguma tenha sido processada fora de ordem
    if DOCS.exists():
        shutil.rmtree(DOCS)
    arquivos = {}

    nomes = {}
    for i, e in ents.items():
        for n in [e["nome"], *e["apelidos"]]:
            if len(n) >= 3:
                nomes.setdefault(n, i)
    padrao = (re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, sorted(nomes, key=len, reverse=True)))
                         + r")(?![\w-])") if nomes else None)

    def rel(de, para):
        return posixpath.relpath(para, posixpath.dirname(de) or ".")

    def ligar(texto, de, usados):
        """Transforma a primeira menção de cada nome conhecido em link para a página dele."""
        texto = seguro(texto)
        if not padrao:
            return texto

        def troca(m):
            i = nomes[m[1]]
            if i in usados:
                return m[1]
            usados.add(i)
            return f"[{m[1]}]({rel(de, i + '.md')})"
        return padrao.sub(troca, texto)

    def link_sessao(de, ep):
        titulo = sessoes[ep]["titulo"]
        return f"[{nome_sessao(ep)}: {seguro(titulo)}]({rel(de, f'sessoes/{ep}.md')})"

    def primeira_frase(texto):
        return re.split(r"(?<=[.!?])\s", texto.strip(), maxsplit=1)[0]

    # páginas de cada nome
    for i, e in ents.items():
        de, usados = f"{i}.md", {i}
        pasta, secao, singular = TIPOS[e["tipo"]]
        linhas = [f"| Tipo | [{singular}]({rel(de, f'{pasta}/index.md')}) |"]
        if e["apelidos"]:
            linhas.append(f"| Outros nomes | {seguro(', '.join(e['apelidos']))} |")
        linhas += [f"| {seguro(c)} | {ligar(v, de, set(usados))} |" for c, v in e["atributos"].items()]
        if e["sessoes"]:
            primeira, ultima = min(e["sessoes"], key=ordem), max(e["sessoes"], key=ordem)
            linhas.append(f"| Primeira aparição | [{nome_sessao(primeira)}]({rel(de, f'sessoes/{primeira}.md')}) |")
            if ultima != primeira:
                linhas.append(f"| Última aparição | [{nome_sessao(ultima)}]({rel(de, f'sessoes/{ultima}.md')}) |")
            linhas.append(f"| Aparições | {len(e['sessoes'])} {'sessão' if len(e['sessoes']) == 1 else 'sessões'} |")
        corpo = [f"{SEM_MENU}# {seguro(e['nome'])}", "", '<div class="infobox" markdown>',
                 f'<p class="infobox-titulo">{seguro(e["nome"])}</p>', "", "| | |", "|---|---|", *linhas,
                 "", "</div>", "", ligar(e["resumo"] or "Sem resumo ainda.", de, usados), ""]
        if e["fatos"]:
            corpo += ["## Histórico", ""]
            for ep in sorted(dict.fromkeys(s for s, f in e["fatos"] if f.strip()), key=ordem):
                if ep in sessoes:
                    corpo += [f"### {link_sessao(de, ep)}", ""]
                corpo += [f"- {ligar(f, de, usados)}" for s, f in e["fatos"] if s == ep and f.strip()] + [""]
        arquivos[de] = "\n".join(corpo)

    # índice de cada seção
    for tipo, (pasta, secao, _) in TIPOS.items():
        de = f"{pasta}/index.md"
        itens = sorted(((i, e) for i, e in ents.items() if e["tipo"] == tipo), key=lambda x: slug(x[1]["nome"]))
        corpo = [f"# {secao}", "", f"{paginas_de(len(itens))} nesta seção.", ""]
        if itens:
            corpo += ["| Nome | Resumo | Aparições |", "|---|---|---|"]
            corpo += [f"| [{seguro(e['nome'])}]({rel(de, i + '.md')}) | {seguro(primeira_frase(e['resumo']))} "
                      f"| {len(e['sessoes'])} |" for i, e in itens]
        arquivos[de] = "\n".join(corpo)

    # sessões
    for n, ep in enumerate(lista):
        s, de = sessoes[ep], f"sessoes/{ep}.md"
        usados = set()
        nav = []
        if n > 0:
            nav.append(f"[← {nome_sessao(lista[n - 1])}]({lista[n - 1]}.md)")
        if n < len(lista) - 1:
            nav.append(f"[{nome_sessao(lista[n + 1])} →]({lista[n + 1]}.md)")
        corpo = [f"{SEM_MENU}# {nome_sessao(ep)}: {seguro(s['titulo'])}", "", " · ".join(nav), "",
                 *[ligar(p, de, usados) + "\n" for p in s["resumo"].split("\n") if p.strip()]]
        if s["linha_do_tempo"]:
            corpo += ["## Acontecimentos", "", *[f"1. {ligar(t, de, usados)}" for t in s["linha_do_tempo"]], ""]
        presentes = [i for i in s["entidades"] if i in ents]
        if presentes:
            corpo += ["## Quem e o que aparece", ""]
            for tipo, (_, secao, _) in TIPOS.items():
                grupo = [f"[{seguro(ents[i]['nome'])}]({rel(de, i + '.md')})" for i in presentes
                         if ents[i]["tipo"] == tipo]
                if grupo:
                    corpo.append(f"- **{secao}:** {', '.join(grupo)}")
            corpo.append("")
        abertas = [p for p in pends if p["aberta_em"] == ep]
        resolvidas = [p for p in pends if p["resolvida_em"] == ep]
        if abertas or resolvidas:
            corpo += ["## Pendências", ""]
            corpo += [f"- Aberta: {ligar(p['texto'], de, usados)}" for p in abertas]
            corpo += [f"- Resolvida: {ligar(p['texto'], de, usados)} {seguro(p['como'])}" for p in resolvidas]
            corpo.append("")
        if Path(f"transcricoes/{ep}.md").exists():
            corpo += [f"[Transcrição completa da sessão]({rel(de, f'transcricoes/{ep}.md')})", ""]
        arquivos[de] = "\n".join(corpo)

    arquivos["sessoes/index.md"] = "\n".join(
        ["# Sessões", "", f"{len(lista)} sessões registradas.", "", "| Sessão | Título |", "|---|---|"]
        + [f"| [{nome_sessao(ep)}]({ep}.md) | {seguro(sessoes[ep]['titulo'])} |" for ep in lista])

    # transcrições: uma página por sessão, com âncora em cada fala para o chat linkar a minutagem
    transcricoes = sorted(Path("transcricoes").glob("*.md"), key=lambda t: ordem(t.stem))
    for t in transcricoes:
        texto = t.read_text(encoding="utf-8")
        arquivos[f"dados/transcricoes/{t.stem}.txt"] = texto  # fonte do chat de IA
        de, vistos = f"transcricoes/{t.stem}.md", {}
        linhas = [SEM_MENU.replace("---\n\n", "search:\n  exclude: true\n---\n\n") + f"# Transcrição: {nome_sessao(t.stem)}", ""]
        if t.stem in sessoes:
            linhas += [f"Resumo e páginas citadas: {link_sessao(de, t.stem)}", ""]
        for linha in texto.splitlines()[1:]:  # a primeira linha é o título do arquivo
            m = re.match(r"\[(\d\d):(\d\d):(\d\d)\]", linha)
            linhas.append(seguro(linha))
            if m:
                ancora = "t-" + "".join(m.groups())
                vistos[ancora] = vistos.get(ancora, 0) + 1
                linhas.append(f"{{: #{ancora}{'-' + str(vistos[ancora]) if vistos[ancora] > 1 else ''} }}")
        arquivos[de] = "\n".join(linhas)
    arquivos["transcricoes/index.md"] = "\n".join(
        ["# Transcrições", "", "Texto completo de cada sessão, com horário e quem fala.", "",
         "| Sessão | Título |", "|---|---|"]
        + [f"| [{nome_sessao(t.stem)}]({t.stem}.md) | {seguro(sessoes[t.stem]['titulo']) if t.stem in sessoes else ''} |"
           for t in transcricoes])

    # dados para o chat de IA
    arquivos["dados/wiki.json"] = json.dumps(dados | {"transcricoes": [t.stem for t in transcricoes]},
                                             ensure_ascii=False)
    arquivos["chat.md"] = "\n".join([
        "---", "hide:", "  - navigation", "  - toc", "search:", "  exclude: true", "---", "",
        "# Pergunte à IA", "",
        "Pergunte qualquer coisa sobre a campanha. As respostas vêm da wiki e das transcrições das sessões.", "",
        '<div id="assistente"></div>', "",
        '<script src="https://cdn.jsdelivr.net/npm/marked@18.1.1/lib/marked.umd.js"></script>',
        '<script src="https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js"></script>',
        '<script src="assistente.js"></script>'])
    if Path("wiki/assistente.js").exists():
        arquivos["assistente.js"] = Path("wiki/assistente.js").read_text(encoding="utf-8")

    # linha do tempo
    de, corpo = "linha-do-tempo.md", ["# Linha do tempo", ""]
    for ep in lista:
        usados = set()
        corpo += [f"## {link_sessao(de, ep)}", "",
                  *[f"1. {ligar(t, de, usados)}" for t in sessoes[ep]["linha_do_tempo"]], ""]
    arquivos[de] = "\n".join(corpo)

    # pendências
    de = "pendencias.md"
    abertas = [p for p in pends if not p["resolvida_em"]]
    resolvidas = [p for p in pends if p["resolvida_em"]]
    corpo = ["# Pendências", "", f"## Em aberto ({len(abertas)})", ""]
    corpo += [f"- {ligar(p['texto'], de, set())} *(desde a {link_sessao(de, p['aberta_em'])})*"
              for p in reversed(abertas) if p["aberta_em"] in sessoes] or ["Nenhuma pendência em aberto."]
    corpo += ["", f"## Resolvidas ({len(resolvidas)})", ""]
    corpo += [f"- ~~{seguro(p['texto'])}~~ Resolvida na {link_sessao(de, p['resolvida_em'])}. {seguro(p['como'])}"
              for p in reversed(resolvidas) if p["resolvida_em"] in sessoes]
    arquivos[de] = "\n".join(corpo)

    # página inicial
    de = "index.md"
    intro = Path("wiki/inicio.md")
    corpo = ["---", "hide:", "  - navigation", "  - toc", "---", "",
             intro.read_text(encoding="utf-8") if intro.exists() else "# Wiki da campanha", ""]
    recap = dados.get("recapitulacao")
    if recap and recap["apos"] in sessoes:
        corpo += ['<div class="recap" markdown>', "",
                  f"**Para a próxima sessão** (situação depois da {link_sessao(de, recap['apos'])})", "",
                  ligar(recap["texto"], de, set()), "", "</div>", ""]
    corpo.append('<div class="secoes">')
    contagem = {t: sum(e["tipo"] == t for e in ents.values()) for t in TIPOS}
    atalhos = [(f"{p}/index.html", s, paginas_de(contagem[t])) for t, (p, s, _) in TIPOS.items()]
    atalhos += [("sessoes/index.html", "Sessões", f"{len(lista)} sessões" if len(lista) != 1 else "1 sessão"),
                ("linha-do-tempo.html", "Linha do tempo", f"{sum(len(s['linha_do_tempo']) for s in sessoes.values())} acontecimentos"),
                ("pendencias.html", "Pendências", f"{len(abertas)} em aberto"),
                ("transcricoes/index.html", "Transcrições", f"{len(transcricoes)} sessões"),
                ("chat.html", "Pergunte à IA", "chat sobre a campanha")]
    corpo += [f'<a class="secao" href="{h}"><strong>{s}</strong><span>{c}</span></a>' for h, s, c in atalhos]
    corpo += ["</div>", ""]
    if lista:
        ultima = lista[-1]
        corpo += ["## Última sessão", "", f"### {link_sessao(de, ultima)}", "",
                  ligar(sessoes[ultima]["resumo"].split("\n")[0], de, set()), ""]
    pcs = [(i, e) for i, e in ents.items() if e["tipo"] == "personagem"]
    if pcs:
        corpo += ["## Personagens dos jogadores", ""]
        corpo += [f"- [{seguro(e['nome'])}]({i}.md)" + (f" ({seguro(e['atributos']['Jogador'])})"
                  if e["atributos"].get("Jogador") else "") for i, e in pcs]
        corpo.append("")
    if abertas:
        corpo += ["## Pendências em aberto", ""]
        corpo += [f"- {ligar(p['texto'], de, set())}" for p in list(reversed(abertas))[:8]]
        corpo += ["", "[Ver todas as pendências](pendencias.md)", ""]
    citados = sorted((x for x in ents.items() if x[1]["tipo"] != "personagem"), key=lambda x: -len(x[1]["sessoes"]))[:12]
    if citados:
        corpo += ["## Mais citados", ""]
        corpo += [f"- [{seguro(e['nome'])}]({i}.md): {seguro(primeira_frase(e['resumo']))}" for i, e in citados]
    arquivos[de] = "\n".join(corpo)

    arquivos["estilo.css"] = ESTILO
    for caminho, conteudo in arquivos.items():
        destino = DOCS / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(conteudo + "\n", encoding="utf-8")
    print(f"{len(arquivos)} arquivos gerados em {DOCS}")


if __name__ == "__main__":
    if sys.argv[1] == "atualizar":
        atualizar(sys.argv[2])
    else:
        paginas()
