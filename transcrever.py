"""Transcreve um zip com uma faixa de áudio por pessoa usando o Whisper do Groq.

Aceita o zip do Craig (com info.txt) ou um zip com arquivos nomeados "Jogador - Personagem.flac".
Uso: python transcrever.py <arquivo.zip> <saida.md>
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SR = 16000  # o Whisper converte tudo para 16 kHz mono
BLOCO_MAX = 600  # segundos por envio, em FLAC fica abaixo do limite de 25 MB do Groq
PAUSA = 1.5  # silêncio colocado entre trechos de fala dentro de um bloco
SILENCIO = bytes(int(PAUSA * SR) * 2)
ALUCINACOES = re.compile(r"amara\.org|legendas pela comunidade|obrigad[oa] por assistir|inscreva-se", re.I)
AUDIO = re.compile(r"\.(aac|flac|oga|ogg|opus|mp3|wav|m4a)$", re.I)
MODELOS = ["whisper-large-v3", "whisper-large-v3-turbo"]  # cada modelo tem a sua própria cota no Groq
LIVRE = {m: 0.0 for m in MODELOS}  # horário em que cada modelo volta a aceitar envios
PRAZO = float("inf")  # o processar.py define até quando o job pode esperar


class Pausa(Exception):
    """Limite do serviço atingido. O trabalho continua na próxima execução."""


def ler_pares(arquivo):
    """Lê linhas 'chave = valor' (jogadores.txt)."""
    if not Path(arquivo).exists():
        return {}
    pares = {}
    for linha in Path(arquivo).read_text(encoding="utf-8").splitlines():
        if "=" in linha and not linha.strip().startswith("#"):
            chave, valor = linha.split("=", 1)
            pares[chave.strip()] = valor.strip()
    return pares


def ler_info(texto):
    """Extrai do info.txt do Craig: usuários por faixa, horário de início e notas."""
    usuarios, notas, inicio, secao = [], [], "", None
    for linha in texto.splitlines():
        if linha.startswith("Start time:"):
            inicio = linha.split(":", 1)[1].strip()
        elif linha.strip() in ("Tracks:", "Notes:"):
            secao = linha.strip()
        elif linha.startswith("\t") and secao == "Tracks:":
            usuarios.append(linha.strip().rsplit(" (", 1)[0].split("#")[0])
        elif linha.startswith("\t") and secao == "Notes:":
            m = re.match(r"\t(\d+):(\d+):([\d.]+): (.*)", linha)
            if m:
                notas.append((int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), m[4]))
    return usuarios, inicio, notas


def ler_elenco(notas, jogadores):
    """Notas 'elenco: Jogador=Personagem, ...' viram [(segundo, usuario em minúsculas, personagem)]."""
    nomes = {k.lower(): k for k in jogadores} | {v.lower(): k for k, v in jogadores.items()}
    mudancas = []
    for seg, texto in notas:
        m = re.match(r"\s*elenco\s*:(.*)", texto, re.I)
        for par in (m[1].split(",") if m else []):
            if "=" in par:
                quem, personagem = (p.strip() for p in par.split("=", 1))
                mudancas.append((seg, nomes.get(quem.lower(), quem).lower(), personagem))
    return sorted(mudancas)


def personagem_em(mudancas, usuario, t):
    """A primeira nota vale desde o início da sessão, as seguintes a partir do seu horário."""
    atual = None
    for seg, usr, personagem in mudancas:
        if usr == usuario.lower() and (seg <= t or atual is None):
            atual = personagem
    return atual


def decodificar(caminho):
    """Converte a faixa em PCM 16 kHz mono e devolve (pcm, trechos com fala)."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", caminho, "-af", "silencedetect=noise=-45dB:d=0.8",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        capture_output=True, check=True)
    log = r.stderr.decode(errors="replace")
    duracao = len(r.stdout) / 2 / SR
    inicios = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    fins = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    fins += [duracao] * (len(inicios) - len(fins))
    trechos, pos = [], 0.0
    for ini, fim in zip(inicios, fins):
        if ini > pos:
            trechos.append((pos, ini))
        pos = fim
    if pos < duracao:
        trechos.append((pos, duracao))
    # margem de 0,25 s, junta trechos próximos e descarta estalos curtos
    unidos = []
    for ini, fim in trechos:
        ini, fim = max(0.0, ini - 0.25), min(duracao, fim + 0.25)
        if unidos and ini - unidos[-1][1] < 0.5:
            unidos[-1] = (unidos[-1][0], fim)
        else:
            unidos.append((ini, fim))
    return r.stdout, [(a, b) for a, b in unidos if b - a >= 0.4]


def blocos(pcm, trechos):
    """Agrupa trechos de fala em blocos de até BLOCO_MAX s. mapa = [(segundo no bloco, segundo real)]."""
    partes, mapa, dur = [], [], 0.0
    for ini, fim in trechos:
        while fim - ini > 0.01:
            pedaco = min(fim - ini, BLOCO_MAX - PAUSA)
            if partes and dur + pedaco > BLOCO_MAX:
                yield b"".join(partes), mapa
                partes, mapa, dur = [], [], 0.0
            mapa.append((dur, ini))
            partes += [pcm[int(ini * SR) * 2:int((ini + pedaco) * SR) * 2], SILENCIO]
            dur += pedaco + PAUSA
            ini += pedaco
    if partes:
        yield b"".join(partes), mapa


def transcrever_bloco(pcm, prompt):
    """Envia um bloco ao Groq. No limite gratuito, troca de modelo ou espera a cota voltar."""
    flac = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SR),
                           "-ac", "1", "-i", "pipe:0", "-f", "flac", "pipe:1"],
                          input=pcm, capture_output=True, check=True).stdout
    for _ in range(60):
        modelo = min(MODELOS, key=LIVRE.get)
        espera = LIVRE[modelo] - time.time()
        if espera > 0:
            if time.time() + espera > PRAZO:
                raise Pausa(f"Cota do Groq volta em {espera / 60:.0f} min, depois do fim deste job")
            print(f"Cota do Groq esgotada, aguardando {espera / 60:.0f} min", flush=True)
            time.sleep(espera)
        try:
            r = requests.post(
                GROQ_URL, timeout=300,
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                files={"file": ("bloco.flac", flac, "audio/flac")},
                data={"model": modelo, "language": "pt", "response_format": "verbose_json",
                      "temperature": "0", "prompt": prompt})
        except requests.RequestException as erro:
            print(f"Falha de conexão ({erro}), tentando de novo em 60 s", flush=True)
            time.sleep(60)
            continue
        if r.status_code == 200:
            return r.json().get("segments", [])
        if r.status_code == 429 or r.status_code >= 500:
            try:
                espera = float(r.headers.get("retry-after", 60))
            except ValueError:
                espera = 60.0
            print(f"Groq respondeu {r.status_code} para {modelo}, cota volta em {espera:.0f} s", flush=True)
            LIVRE[modelo] = time.time() + espera + 1
            continue
        raise RuntimeError(f"Groq recusou o envio ({r.status_code}): {r.text[:300]}")
    raise RuntimeError("Groq não respondeu após 60 tentativas")


def hms(seg):
    seg = int(seg)
    return f"{seg // 3600:02d}:{seg % 3600 // 60:02d}:{seg % 60:02d}"


def nomes_do_prompt():
    """Glossário manual primeiro, depois os nomes mais citados na wiki."""
    nomes = []
    if Path("glossario.txt").exists():
        nomes += [l.strip() for l in Path("glossario.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
    if Path("wiki/dados.json").exists():
        entidades = json.loads(Path("wiki/dados.json").read_text(encoding="utf-8"))["entidades"].values()
        nomes += [e["nome"] for e in sorted(entidades, key=lambda e: -len(e["sessoes"]))]
    return ", ".join(dict.fromkeys(nomes))[:600]  # o Whisper aceita cerca de 224 tokens de prompt


def faixas(z):
    """Lista (arquivo, usuário, personagem do nome do arquivo) de um zip, mais início e notas do info.txt."""
    info = next((n for n in z.namelist() if Path(n).name == "info.txt"), None)
    usuarios, inicio, notas = ler_info(z.read(info).decode("utf-8", "replace") if info else "")
    lista = []
    for nome in sorted(n for n in z.namelist() if AUDIO.search(n) and not Path(n).name.startswith(".")):
        m = re.match(r"(\d+)-(.+)", Path(nome).stem)
        usuario, personagem = m[2] if m else Path(nome).stem, None
        if " - " in usuario:  # arquivo "Jogador - Personagem"
            usuario, personagem = (p.strip() for p in usuario.split(" - ", 1))
        if m and info and int(m[1]) <= len(usuarios):
            usuario = usuarios[int(m[1]) - 1]  # zip do Craig: a faixa N é o usuário N do info.txt
        lista.append((nome, usuario, personagem))
    return lista, inicio, notas


def transcrever_faixa(caminho, usuario, prompt):
    """Devolve [(início, fim, usuario, texto)] de uma faixa, com horários reais da sessão."""
    pcm, trechos = decodificar(caminho)
    print(f"  {sum(b - a for a, b in trechos) / 60:.1f} min com fala", flush=True)
    falas = []
    for audio, mapa in blocos(pcm, trechos):
        for s in transcrever_bloco(audio, prompt):
            texto = s.get("text", "").strip()
            if (not texto or ALUCINACOES.search(texto) or s.get("compression_ratio", 0) > 2.4
                    or (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1)):
                continue
            base, real = mapa[0]
            for b, r in mapa:
                if b <= s["start"] + PAUSA / 2:
                    base, real = b, r
            t = real + max(0.0, s["start"] - base)
            falas.append((t, t + s["end"] - s["start"], usuario, texto))
    return falas


def transcrever(zips, saida, elenco=""):
    """zips: um arquivo ou uma lista de partes da mesma sessão."""
    zips = [zips] if isinstance(zips, str) else zips
    jogadores = {k.lower(): v for k, v in ler_pares("jogadores.txt").items()}
    prompt = nomes_do_prompt()

    falas, notas, inicio, fixos = [], [(0.0, l) for l in elenco.splitlines()], "", []
    with tempfile.TemporaryDirectory() as tmp:
        for zip_path in zips:
            with zipfile.ZipFile(zip_path) as z:
                lista, inicio_zip, notas_zip = faixas(z)
                inicio, notas = inicio or inicio_zip, notas + notas_zip
                for nome, usuario, personagem in lista:
                    if personagem:
                        fixos.append((0.0, usuario.lower(), personagem))
                    print(f"Faixa {nome}: {usuario}", flush=True)
                    caminho = z.extract(nome, tmp)
                    falas += transcrever_faixa(caminho, usuario, prompt)
                    os.remove(caminho)
    mudancas = fixos + ler_elenco(notas, jogadores)  # nome do arquivo vale desde o início, notas depois

    linhas, anterior = [], None
    for ini, fim, usuario, texto in sorted(falas):
        jogador = jogadores.get(usuario.lower(), usuario)
        personagem = personagem_em(mudancas, usuario, ini)
        rotulo = f"{personagem} ({jogador})" if personagem else jogador
        if anterior and anterior[1] == rotulo and ini - anterior[2] < 2:
            anterior[3].append(texto)
            anterior[2] = max(anterior[2], fim)
        else:
            anterior = [ini, rotulo, fim, [texto]]
            linhas.append(anterior)

    corpo = "\n\n".join(f"[{hms(ini)}] {rotulo}: {' '.join(textos)}" for ini, rotulo, _, textos in linhas)
    Path(saida).parent.mkdir(parents=True, exist_ok=True)
    cabecalho = f"# {Path(saida).stem}\n\n" + (f"Gravação iniciada em {inicio}\n\n" if inicio else "")
    Path(saida).write_text(f"{cabecalho}{corpo}\n", encoding="utf-8")
    print(f"Salvo em {saida}: {len(linhas)} falas", flush=True)


if __name__ == "__main__":
    transcrever(sys.argv[1], sys.argv[2], os.environ.get("ELENCO", ""))
