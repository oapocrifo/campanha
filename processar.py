"""Fila da campanha: transcreve os zips publicados nas releases e atualiza a wiki, sempre em ordem de sessão.

Cada passo é salvo no repositório na hora. Se um limite gratuito acabar, o job para e a próxima execução continua.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import revisar
import transcrever
import wiki

INICIO = time.time()
transcrever.PRAZO = INICIO + 5 * 3600  # o GitHub encerra o job em 6 h
ULTIMO_INICIO = INICIO + 4 * 3600  # depois disso não começa episódio novo


def sh(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def salvar(mensagem):
    caminhos = [p for p in ("transcricoes", "wiki/dados.json", "wiki/correcoes.txt", "wiki/revisao.json")
                if Path(p).exists()]
    sh("git", "add", "-A", *caminhos)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode:
        sh("git", "commit", "-m", mensagem)
        sh("git", "pull", "--rebase")
        sh("git", "push")


def zips_por_release():
    """{tag: (descrição, [zips])} das releases publicadas."""
    releases = {}
    for rel in json.loads(sh("gh", "release", "list", "--limit", "200", "--json", "tagName,isDraft")):
        if not rel["isDraft"]:
            info = json.loads(sh("gh", "release", "view", rel["tagName"], "--json", "body,assets"))
            zips = [a["name"] for a in info["assets"] if a["name"].lower().endswith(".zip")]
            releases[rel["tagName"]] = (info["body"] or "", zips)
    return releases


SESSAO = r"sess[aã]o[-_ ]?(\d+)"


def padrao(nome):
    """"Sessão 7", "sessão-001" e "sessao_07" viram "sessao-07": o mesmo episódio sempre com o mesmo nome."""
    m = re.fullmatch(SESSAO, nome.strip(), re.I)
    return f"sessao-{int(m[1]):02d}" if m else nome


def fila(releases, transcritas, na_wiki):
    """Sessões pendentes: {sessao: (tag, [zips], descrição)}.

    Zip chamado "sessao-07.zip" (ou "sessao-07 parte 1.zip") é a sessão sessao-07.
    Qualquer outro nome, como o zip do Craig, vira a sessão com o nome da tag da release.
    """
    pendentes = {}
    for tag, (descricao, zips) in releases.items():
        for z in zips:
            base = re.sub(r"[\s_-]*parte[\s_-]*\d+$", "", z[:-4], flags=re.I)
            ep = padrao(base) if re.fullmatch(SESSAO, base, re.I) else padrao(tag)
            pendentes.setdefault(ep, (tag, [], descricao))[1].append(z)
    sessoes = sorted(set(pendentes) | (transcritas - na_wiki), key=wiki.ordem)
    return sessoes, pendentes


def main():
    releases = zips_por_release()
    transcritas = {p.stem for p in Path("transcricoes").glob("*.md")}
    sessoes, pendentes = fila(releases, transcritas, set(wiki.carregar()["sessoes"]))
    print("Fila:", ", ".join(sessoes) or "vazia", flush=True)
    # a wiki segue a ordem das sessões: se ela parar numa sessão, a transcrição das seguintes continua
    wiki_parada = None if os.environ.get("GEMINI_API_KEY") else "a chave GEMINI_API_KEY não está cadastrada no GitHub"
    erro_wiki = False
    try:
        for ep in sessoes:
            if time.time() > ULTIMO_INICIO:
                print("Pouco tempo restante neste job. A próxima execução continua.", flush=True)
                break
            saida = f"transcricoes/{ep}.md"
            if ep in pendentes:
                tag, partes, descricao = pendentes[ep]
                if not Path(saida).exists():
                    with tempfile.TemporaryDirectory() as tmp:
                        for z in partes:
                            sh("gh", "release", "download", tag, "--pattern", z, "--dir", tmp)
                        transcrever.transcrever([f"{tmp}/{z}" for z in sorted(partes, key=wiki.ordem)],
                                                saida, descricao)
                    salvar(f"Transcrição {ep}")
                for z in partes:
                    sh("gh", "release", "delete-asset", tag, z, "--yes")
                    releases[tag][1].remove(z)
                if not releases[tag][1]:
                    sh("gh", "release", "delete", tag, "--yes", "--cleanup-tag")
            if wiki_parada:
                continue
            try:
                wiki.atualizar(saida)
            except (RuntimeError, wiki.Pausa) as e:
                wiki_parada, erro_wiki = f"{ep}: {e}", isinstance(e, RuntimeError)
                continue
            salvar(f"Wiki: {ep}")
    except transcrever.Pausa as pausa:
        print(f"Pausa: {pausa}. A próxima execução continua de onde parou.", flush=True)
    if wiki_parada:
        print(f"Wiki parada nesta execução ({wiki_parada})", flush=True)
    # revisão periódica: só com a fila vazia, para não revisar uma wiki incompleta
    falta = ({p.stem for p in Path("transcricoes").glob("*.md")} | set(pendentes)) - set(wiki.carregar()["sessoes"])
    if not wiki_parada and not falta and revisar.pendente(os.environ.get("REVISAR_AGORA") == "true"):
        try:
            relatorio = revisar.revisar()
            if relatorio:
                revisar.abrir_issue(relatorio)
            salvar("Revisão da wiki")
        except (RuntimeError, wiki.Pausa) as e:
            print(f"Revisão adiada: {e}", flush=True)
    if erro_wiki:
        sys.exit("Erro na wiki. A transcrição seguiu normalmente, e a wiki tenta de novo na próxima execução.")


if __name__ == "__main__":
    main()
