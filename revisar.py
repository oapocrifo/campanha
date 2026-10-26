"""Revisão periódica da wiki.

O Gemini lê a wiki inteira e aponta inconsistências. As correções simples são aplicadas aqui
(no máximo MAX_CORRECOES por revisão). Contradições são conferidas nas transcrições antes de decidir.
O que precisa de decisão humana vira uma issue no GitHub.
"""
import json
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import wiki

ESTADO = Path("wiki/revisao.json")  # data da última revisão e problemas já reportados
CORRECOES = Path("wiki/correcoes.txt")
PROTEGIDAS = Path("wiki/protegidas.txt")
INTERVALO_DIAS = 7
MAX_CORRECOES = 30
MAX_VERIFICACOES = 5
PARTE = 200_000  # caracteres por envio ao Gemini, para caber no limite gratuito por minuto

AUTOMATICOS = ["duplicata", "grafia", "tipo_errado", "resumo", "pendencia_resolvida"]
NOMES = {"duplicata": "Duplicata", "grafia": "Grafia", "tipo_errado": "Seção errada", "resumo": "Resumo",
         "pendencia_resolvida": "Pendência resolvida", "contradicao": "Contradição",
         "identidade_incerta": "Identidade incerta", "pagina_inutil": "Página desnecessária", "outro": "Outro"}

INSTRUCOES = """Você revisa a wiki de uma campanha de RPG de mesa, gerada automaticamente a partir das sessões.
Procure inconsistências e devolva uma lista de problemas. Tipos:
- duplicata: duas páginas sobre exatamente a mesma coisa. ids = [página que deve sumir, página que fica].
- grafia: nome de página escrito errado, quando o certo aparece em outras páginas ou nos fatos. ids = [página], valor = nome correto.
- tipo_errado: página na seção errada. ids = [página], valor = personagem (só personagens dos jogadores), npc, criatura, local, faccao, item ou evento.
- resumo: resumo da página que contradiz ou deixa de fora o que o histórico dela diz. ids = [página], valor = novo resumo em até 4 frases, baseado só no histórico.
- pendencia_resolvida: pendência aberta que uma sessão posterior resolveu. ids = [id da pendência], sessao = sessão que resolveu, valor = como foi resolvida, em uma frase.
- contradicao: informações que se contradizem entre páginas ou sessões (por exemplo, morto numa sessão e vivo numa sessão posterior sem explicação). ids = páginas envolvidas.
- identidade_incerta: páginas que talvez sejam a mesma coisa, sem certeza. ids = páginas.
- pagina_inutil: página sem importância para a história da campanha. ids = [página].
- outro: qualquer outro problema. ids = páginas envolvidas.
Regras:
- Aponte só problemas reais, com base no conteúdo recebido. Não invente.
- Seja conservador: na dúvida entre uma correção (duplicata, grafia, tipo_errado, resumo, pendencia_resolvida) e um caso para decisão humana (contradicao, identidade_incerta, outro), escolha o caso para decisão humana.
- Personagens dos jogadores com nomes parecidos são pessoas diferentes, salvo prova clara no conteúdo.
- descricao: uma ou duas frases explicando o problema e citando as sessões.
- Use exatamente os ids recebidos. Escreva em português do Brasil."""

TEXTO = {"type": "STRING"}
ESQUEMA = {"type": "OBJECT", "required": ["problemas"], "properties": {"problemas": {"type": "ARRAY", "items": {
    "type": "OBJECT", "required": ["tipo", "ids", "descricao"],
    "properties": {"tipo": {"type": "STRING", "enum": list(NOMES)}, "ids": {"type": "ARRAY", "items": TEXTO},
                   "descricao": TEXTO, "valor": TEXTO, "sessao": TEXTO}}}}}

INSTRUCOES_VERIFICAR = """Você confere uma possível contradição na wiki de uma campanha de RPG de mesa.
Recebe o problema, as páginas envolvidas e as falas das transcrições que citam esses nomes, no formato [sessão hh:mm:ss] Personagem (Jogador): fala. O Mestre narra e interpreta os NPCs.
Decida com base nas falas:
- Se a wiki estiver errada e a correção for só reescrever o resumo de uma das páginas, devolva corrigir = true, id da página e novo_resumo (até 4 frases).
- Se não houver contradição de verdade, devolva corrigir = false e inexistente = true.
- Nos outros casos, devolva corrigir = false e explique o problema para o mestre decidir.
explicacao: até 3 frases. evidencias: até 4 falas que mostram o problema, copiadas no formato recebido. Não invente falas."""

ESQUEMA_VERIFICAR = {"type": "OBJECT", "required": ["corrigir", "explicacao"], "properties": {
    "corrigir": {"type": "BOOLEAN"}, "inexistente": {"type": "BOOLEAN"}, "id": TEXTO, "novo_resumo": TEXTO,
    "explicacao": TEXTO, "evidencias": {"type": "ARRAY", "items": TEXTO}}}


def carregar_estado():
    return json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {"ultima": None, "reportados": []}


def pendente(forcar=False):
    """Está na hora de revisar?"""
    ultima = carregar_estado()["ultima"]
    return forcar or not ultima or (date.today() - date.fromisoformat(ultima)).days >= INTERVALO_DIAS


def protegidos(dados):
    """ids das páginas listadas em wiki/protegidas.txt, que o robô não altera."""
    if not PROTEGIDAS.exists():
        return set()
    nomes = {l.strip().lower() for l in PROTEGIDAS.read_text(encoding="utf-8").splitlines() if l.strip()}
    return {i for i, e in dados["entidades"].items() if {e["nome"].lower(), *map(str.lower, e["apelidos"])} & nomes}


def partes_da_wiki(dados):
    """Divide a wiki em envios de até PARTE caracteres. Todo envio leva o índice de nomes, para achar duplicatas."""
    indice = "ÍNDICE DE PÁGINAS (id | tipo | nome | outros nomes):\n" + "\n".join(
        f"{i} | {e['tipo']} | {e['nome']} | {', '.join(e['apelidos'])}" for i, e in dados["entidades"].items())
    blocos = [json.dumps({"id": i, "tipo": e["tipo"], "nome": e["nome"], "outros_nomes": e["apelidos"],
                          "resumo": e["resumo"], "atributos": e["atributos"],
                          "historico": [f"{s}: {f}" for s, f in e["fatos"] if f.strip()]}, ensure_ascii=False)
              for i, e in dados["entidades"].items()]
    partes, atual = [], []
    for b in blocos:
        if atual and sum(map(len, atual)) + len(b) > PARTE:
            partes.append("PÁGINAS:\n" + "\n".join(atual))
            atual = []
        atual.append(b)
    if atual:
        partes.append("PÁGINAS:\n" + "\n".join(atual))
    sessoes = "\n".join(f"{ep}: {s['titulo']}. {s['resumo']} Acontecimentos: {' '.join(s['linha_do_tempo'])}"
                        for ep, s in sorted(dados["sessoes"].items(), key=lambda x: wiki.ordem(x[0])))
    pendencias = "\n".join(f"{p['id']} (aberta em {p['aberta_em']}): {p['texto']}"
                           for p in dados["pendencias"] if not p["resolvida_em"])
    partes.append(f"RESUMOS DAS SESSÕES:\n{sessoes}\n\nPENDÊNCIAS EM ABERTO:\n{pendencias}")
    return [f"{indice}\n\n{p}" for p in partes]


def mencoes(dados, ids):
    """Falas das transcrições que citam os nomes das páginas, no formato [sessão hh:mm:ss] quem: fala."""
    nomes = [n for i in ids if i in dados["entidades"]
             for n in [dados["entidades"][i]["nome"], *dados["entidades"][i]["apelidos"]] if len(n) >= 3]
    if not nomes:
        return []
    padrao = re.compile("|".join(re.escape(n) for n in nomes), re.I)
    falas = []
    for t in sorted(Path("transcricoes").glob("*.md"), key=lambda t: wiki.ordem(t.stem)):
        for linha in t.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\[(\d\d:\d\d:\d\d)\] (.*)", linha)
            if m and padrao.search(m[2]):
                falas.append(f"[{t.stem} {m[1]}] {m[2][:300]}")
    if len(falas) > 300:  # amostra espalhada por todas as sessões
        falas = falas[::len(falas) // 300 + 1]
    return falas


def chave(p):
    return f"{p['tipo']}:{','.join(sorted(p['ids']))}"


def site():
    m = re.search(r"^site_url:\s*(\S+)", Path("wiki/mkdocs.yml").read_text(encoding="utf-8"), re.M) \
        if Path("wiki/mkdocs.yml").exists() else None
    return m[1].rstrip("/") + "/" if m else ""


def link(dados, i):
    e = dados["entidades"].get(i)
    if not e:
        return f"`{i}`"
    return f"[{e['nome']}]({site()}{i}.html)" if site() else f"**{e['nome']}**"


def evidencia(texto):
    """[sessao-10 00:12:03] fala vira link para a minutagem quando o endereço do site é conhecido."""
    m = re.match(r"\[(\S+) (\d\d):(\d\d):(\d\d)\] (.*)", texto)
    if not m:
        return texto
    rotulo = f"{wiki.nome_sessao(m[1])}, {m[2]}:{m[3]}:{m[4]}"
    if site():
        rotulo = f"[{rotulo}]({site()}transcricoes/{m[1]}.html#t-{m[2]}{m[3]}{m[4]})"
    return f"{rotulo}: {m[5]}"


def aplicar(dados, p, linhas_correcao):
    """Aplica uma correção simples. Devolve a frase para o relatório, ou None se não deu para aplicar."""
    ents, ids, valor = dados["entidades"], p["ids"], (p.get("valor") or "").strip()
    if p["tipo"] == "duplicata" and len(ids) == 2 and all(i in ents for i in ids) and ids[0] != ids[1]:
        linhas_correcao.append(f"{ents[ids[0]]['nome']} = {ents[ids[1]]['nome']}")
        return f"\"{ents[ids[0]]['nome']}\" juntada em {link(dados, ids[1])}"
    if p["tipo"] == "grafia" and len(ids) == 1 and ids[0] in ents and valor and valor != ents[ids[0]]["nome"]:
        linhas_correcao.append(f"{ents[ids[0]]['nome']} = {valor}")
        return f"\"{ents[ids[0]]['nome']}\" passa a se chamar \"{valor}\""
    if p["tipo"] == "tipo_errado" and len(ids) == 1 and ids[0] in ents and valor in wiki.TIPOS \
            and valor != ents[ids[0]]["tipo"]:
        antes = ents[ids[0]]["tipo"]
        ents[ids[0]]["tipo"] = valor
        return f"{link(dados, ids[0])} passou de {wiki.TIPOS[antes][1]} para {wiki.TIPOS[valor][1]}"
    if p["tipo"] == "resumo" and len(ids) == 1 and ids[0] in ents and valor:
        ents[ids[0]]["resumo"] = valor
        return f"resumo de {link(dados, ids[0])} reescrito"
    if p["tipo"] == "pendencia_resolvida" and len(ids) == 1 and p.get("sessao") in dados["sessoes"]:
        for pend in dados["pendencias"]:
            if pend["id"] == ids[0] and not pend["resolvida_em"]:
                pend.update(resolvida_em=p["sessao"], como=valor)
                return f"pendência \"{pend['texto']}\" marcada como resolvida na {wiki.nome_sessao(p['sessao'])}"
    return None


def revisar():
    """Faz uma revisão completa. Devolve o texto do relatório, ou None se não encontrou nada."""
    dados = wiki.carregar()
    if not dados["sessoes"]:
        return None
    estado = carregar_estado()
    ja = set(estado["reportados"])
    travadas = protegidos(dados)

    problemas = []
    for n, parte in enumerate(partes_da_wiki(dados), 1):
        print(f"Revisão: enviando a parte {n} ao Gemini ({len(parte)} caracteres)", flush=True)
        problemas += wiki.gemini(parte, INSTRUCOES, ESQUEMA)["problemas"]
    print(f"Revisão: {len(problemas)} problemas apontados", flush=True)

    corrigidos, decidir, linhas_correcao, verificados, nesta = [], [], [], 0, set()
    for p in problemas:
        p["ids"] = [i.strip() for i in p["ids"] if i.strip()]
        if not p["ids"] or chave(p) in ja or chave(p) in nesta:  # partes diferentes podem apontar o mesmo problema
            continue
        nesta.add(chave(p))
        if p["tipo"] in AUTOMATICOS and not travadas & set(p["ids"]) and len(corrigidos) < MAX_CORRECOES:
            feito = aplicar(dados, p, linhas_correcao)
            if feito:
                corrigidos.append(f"- **{NOMES[p['tipo']]}:** {feito}. {p['descricao']}")
                continue
        evidencias = []
        if p["tipo"] == "contradicao" and verificados < MAX_VERIFICACOES:
            verificados += 1
            falas = mencoes(dados, p["ids"])
            paginas = "\n\n".join(json.dumps({k: dados["entidades"][i][k] for k in ("nome", "resumo", "atributos", "fatos")},
                                             ensure_ascii=False) for i in p["ids"] if i in dados["entidades"])
            v = wiki.gemini(f"PROBLEMA:\n{p['descricao']}\n\nPÁGINAS:\n{paginas}\n\nFALAS:\n" + "\n".join(falas),
                            INSTRUCOES_VERIFICAR, ESQUEMA_VERIFICAR)
            if v.get("inexistente") and not v["corrigir"]:
                continue
            i = (v.get("id") or "").strip()
            if v["corrigir"] and i in dados["entidades"] and i not in travadas and (v.get("novo_resumo") or "").strip() \
                    and len(corrigidos) < MAX_CORRECOES:
                dados["entidades"][i]["resumo"] = v["novo_resumo"].strip()
                corrigidos.append(f"- **Contradição:** resumo de {link(dados, i)} corrigido com base nas transcrições. "
                                  f"{v['explicacao']}")
                continue
            p["descricao"] = f"{p['descricao']} {v['explicacao']}"
            evidencias = v.get("evidencias") or []
        motivo = " (página protegida)" if travadas & set(p["ids"]) and p["tipo"] in AUTOMATICOS else ""
        item = (f"- [ ] **{NOMES[p['tipo']]}{motivo}** em {', '.join(link(dados, i) for i in p['ids'])}: {p['descricao']}"
                + (f" Sugestão: {p['valor']}" if p.get("valor") and p["tipo"] in AUTOMATICOS else ""))
        decidir.append(item + "".join(f"\n    - {evidencia(e)}" for e in evidencias[:4]))
        ja.add(chave(p))

    # grava: primeiro as edições diretas, depois as linhas do correcoes.txt (aplicadas ao carregar de novo)
    wiki.DADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if linhas_correcao:
        antes = CORRECOES.read_text(encoding="utf-8") if CORRECOES.exists() else ""
        CORRECOES.write_text(antes + ("" if not antes or antes.endswith("\n") else "\n")
                             + f"# Revisão automática de {date.today()}\n" + "\n".join(linhas_correcao) + "\n",
                             encoding="utf-8")
        dados = wiki.carregar()
        wiki.DADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ESTADO.write_text(json.dumps({"ultima": date.today().isoformat(), "reportados": sorted(ja)},
                                 ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Revisão: {len(corrigidos)} correções, {len(decidir)} casos para decidir", flush=True)
    if not corrigidos and not decidir:
        return None
    partes = [f"Revisão automática da wiki em {date.today().strftime('%d/%m/%Y')}."]
    if corrigidos:
        partes += [f"## Corrigido automaticamente ({len(corrigidos)})", "\n".join(corrigidos)]
    if decidir:
        partes += [f"## Para você decidir ({len(decidir)})", "\n".join(decidir)]
    partes.append("Para resolver um caso, corrija pelo `wiki/correcoes.txt` ou pelo `wiki/dados.json` e marque o item. "
                  "Para desfazer uma correção automática, use o **History** do arquivo. Para o robô não mexer numa página, "
                  "acrescente o nome dela em `wiki/protegidas.txt`. Feche esta issue quando terminar.")
    return "\n\n".join(partes)


def abrir_issue(relatorio):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(relatorio)
    corrigidos = re.search(r"Corrigido automaticamente \((\d+)\)", relatorio)
    decidir = re.search(r"Para você decidir \((\d+)\)", relatorio)
    titulo = (f"Revisão da wiki {date.today().strftime('%d/%m/%Y')}: "
              f"{corrigidos[1] if corrigidos else 0} correções, {decidir[1] if decidir else 0} casos para decidir")
    subprocess.run(["gh", "issue", "create", "--title", titulo, "--body-file", f.name], check=True)
