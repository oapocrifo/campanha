// Chat de IA da wiki. A cada pergunta, busca na wiki e nas transcrições os trechos ligados a ela
// e envia só esses trechos ao modelo escolhido, junto com a pergunta.
(() => {
  const raiz = document.getElementById("assistente");
  if (!raiz) return;

  // caracteres de contexto por pergunta: o Gemini do site aceita 1 milhão de tokens, e 300 mil caracteres
  // (cerca de 85 mil tokens) cabem uma sessão inteira. Modelos do OpenRouter recebem o que cabe no contexto deles.
  const LIMITE_SITE = 300000;
  const contextos = {};
  const PARADAS = new Set(("que qual quais quem como onde quando porque nao sim sobre ate apos entre isso esse essa este esta " +
    "aquele aquela ele ela eles elas lhe meu minha seu sua seus suas nos voce voces foi era sao ser estar tem ter teve " +
    "houve aconteceu acontece acontecer sabemos sabe saber diga fale conte mim qualquer coisa coisas todo toda todos " +
    "todas mais menos muito pouco ainda tambem apenas sessao sessoes episodio episodios campanha para pelo pela pelos pelas com " +
    "sem dos das nas nos uma umas uns por apareceu aparece aparecer apareceram aparicao aparicoes minutagem disponibilize " +
    "dele dela deles delas final inicio fim comeco resuma resumo explique").split(" "));

  const ler = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
  const guardar = (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* sem armazenamento */ } };
  let chave = ler("openrouter_chave");
  let modelo = ler("modelo") || "site";
  const historico = [];
  let dados = null, eps = [], falas = [], blocos = [], docs = [];
  const df = new Map();

  raiz.innerHTML = `
    <div class="conta">
      <label>Modelo <select id="ia-modelo" aria-label="Modelo de IA"></select></label>
      <button type="button" id="ia-conectar"></button>
      <span class="aviso" id="ia-conta"></span>
    </div>
    <div class="mensagens" id="ia-mensagens" aria-live="polite"></div>
    <form class="envio" id="ia-form">
      <textarea id="ia-pergunta" rows="2" aria-label="Pergunta" placeholder="Ex.: O que aconteceu no final da sessão 37?"></textarea>
      <button class="principal" id="ia-enviar" type="submit">Enviar</button>
    </form>
    <p class="aviso">O modelo grátis do site tem um limite diário dividido entre todos os visitantes.
      Conectando sua conta OpenRouter, você usa modelos gratuitos com a sua própria cota ou modelos pagos com os seus créditos.</p>`;
  const $ = (id) => document.getElementById(id);

  // ---------- texto ----------
  const normalizar = (t) => t.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  const termos = (t) => normalizar(t).split(/[^a-z0-9]+/).filter((p) => !PARADAS.has(p))
    .map((p) => (p.length > 4 && p.endsWith("s") ? p.slice(0, -1) : p))
    .filter((p) => p.length >= 3 && !/^\d+$/.test(p));
  const escapar = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const frase = (nome) => new RegExp(`(^|[^a-z0-9])${escapar(normalizar(nome))}([^a-z0-9]|$)`);
  const numero = (ep) => Number((ep.match(/(\d+)\D*$/) || [])[1] || NaN);
  const ordenar = (a, b) => (numero(a) - numero(b)) || a.localeCompare(b);
  const nomeSessao = (ep) => (/^sess[aã]o/i.test(ep) && !isNaN(numero(ep)) ? `Sessão ${numero(ep)}` : ep);
  const contar = (lista) => lista.reduce((m, t) => m.set(t, (m.get(t) || 0) + 1), new Map());

  function pontuar(qt, mapa, total) {
    let s = 0;
    for (const t of qt) {
      const tf = mapa.get(t);
      if (tf) s += Math.log(1 + total / (df.get(t) || 1)) * (tf * 2.2) / (tf + 1.2);
    }
    return s;
  }

  // ---------- dados ----------
  async function carregar() {
    if (dados) return;
    dados = await (await fetch("dados/wiki.json")).json();
    eps = [...new Set([...Object.keys(dados.sessoes), ...dados.transcricoes])].sort(ordenar);
    const textos = await Promise.all(dados.transcricoes.map((ep) =>
      fetch(`dados/transcricoes/${encodeURIComponent(ep)}.txt`).then((r) => (r.ok ? r.text() : ""))));
    textos.forEach((texto, n) => {
      const ep = dados.transcricoes[n], vistos = {}, inicio = falas.length;
      for (const linha of texto.split("\n")) {
        const m = linha.match(/^\[(\d\d):(\d\d):(\d\d)\] (.+?): (.*)$/);
        if (!m) continue;
        let ancora = `t-${m[1]}${m[2]}${m[3]}`;
        vistos[ancora] = (vistos[ancora] || 0) + 1;
        if (vistos[ancora] > 1) ancora += `-${vistos[ancora]}`; // igual às âncoras das páginas de transcrição
        falas.push({ ep, hora: `${m[1]}:${m[2]}:${m[3]}`, ancora, quem: m[4], texto: m[5], norm: normalizar(`${m[4]} ${m[5]}`) });
      }
      for (let i = inicio; i < falas.length; i += 12) {
        const trecho = falas.slice(i, i + 12);
        blocos.push({ ep, trecho, termos: contar(trecho.flatMap((f) => termos(f.texto))) });
      }
    });
    for (const [id, e] of Object.entries(dados.entidades)) {
      const texto = [e.nome, ...e.apelidos, e.resumo, ...Object.values(e.atributos), ...e.fatos.map((f) => f[1])].join(" ");
      docs.push({ tipo: "pagina", id, termos: contar(termos(texto)) });
    }
    for (const [ep, s] of Object.entries(dados.sessoes)) {
      docs.push({ tipo: "sessao", id: ep, termos: contar(termos([s.titulo, s.resumo, ...s.linha_do_tempo].join(" "))) });
    }
    for (const item of [...blocos, ...docs]) for (const t of item.termos.keys()) df.set(t, (df.get(t) || 0) + 1);
  }

  // ---------- montagem do contexto ----------
  const linkFala = (f) => `[${nomeSessao(f.ep)}, ${f.hora}](transcricoes/${f.ep}.html#${f.ancora}) ${f.quem}: ${f.texto.slice(0, 260)}`;
  const linkSessao = (ep) => (dados.sessoes[ep] ? `[${nomeSessao(ep)}: ${dados.sessoes[ep].titulo}](sessoes/${ep}.html)`
    : `[${nomeSessao(ep)}](transcricoes/${ep}.html)`); // sessão ainda sem página na wiki: liga à transcrição
  const transcricaoCompleta = (ep) => `TRANSCRIÇÃO COMPLETA DA ${nomeSessao(ep).toUpperCase()} ` +
    `(link de um horário hh:mm:ss: transcricoes/${ep}.html#t-hhmmss):\n` +
    falas.filter((f) => f.ep === ep).map((f) => `[${f.hora}] ${f.quem}: ${f.texto}`).join("\n");

  function textoSessao(ep) {
    const s = dados.sessoes[ep];
    const linhas = [`SESSÃO: ${linkSessao(ep)} | transcrição completa: transcricoes/${ep}.html`];
    if (s) {
      linhas.push(`Resumo: ${s.resumo}`, "Acontecimentos em ordem:", ...s.linha_do_tempo.map((t, i) => `${i + 1}. ${t}`));
      for (const p of dados.pendencias) {
        if (p.aberta_em === ep) linhas.push(`Pendência aberta nesta sessão: ${p.texto}`);
        if (p.resolvida_em === ep) linhas.push(`Pendência resolvida nesta sessão: ${p.texto} ${p.como}`);
      }
    } else linhas.push("(sessão ainda sem resumo na wiki)");
    return linhas.join("\n");
  }

  function textoPagina(id) {
    const e = dados.entidades[id];
    return [`PÁGINA DA WIKI: [${e.nome}](${id}.html) (tipo: ${e.tipo})`,
      e.apelidos.length ? `Outros nomes: ${e.apelidos.join(", ")}` : "",
      `Resumo: ${e.resumo}`,
      ...Object.entries(e.atributos).map(([c, v]) => `${c}: ${v}`),
      "Histórico:", ...e.fatos.filter((f) => f[1].trim()).map(([ep, f]) => `- ${nomeSessao(ep)}: ${f}`),
      `Sessões em que aparece segundo a wiki: ${[...e.sessoes].sort(ordenar).map(linkSessao).join(", ")}`,
    ].filter(Boolean).join("\n");
  }

  function textoMencoes(rotulo, padroes) {
    const porSessao = new Map();
    for (const f of falas) {
      if (padroes.some((p) => p.test(f.norm))) {
        if (!porSessao.has(f.ep)) porSessao.set(f.ep, []);
        porSessao.get(f.ep).push(f);
      }
    }
    const total = [...porSessao.values()].reduce((s, l) => s + l.length, 0);
    const linhas = [`MENÇÕES A "${rotulo}" NAS TRANSCRIÇÕES: ${total} falas em ${porSessao.size} sessões. Cada linha tem o link da minutagem.`];
    const porEp = Math.max(3, Math.floor(120 / Math.max(1, porSessao.size)));
    for (const [ep, lista] of [...porSessao.entries()].sort((a, b) => ordenar(a[0], b[0]))) {
      linhas.push(`${nomeSessao(ep)}: ${lista.length} falas, a primeira às ${lista[0].hora} e a última às ${lista.at(-1).hora}`);
      linhas.push(...lista.slice(0, porEp).map(linkFala));
    }
    return linhas.join("\n");
  }

  function contexto(pergunta, limite) {
    const q = normalizar(pergunta);
    const qt = [...new Set(termos(pergunta))];
    const partes = [];

    // sessões citadas pelo número ("sessão 37", "sessões 12 e 13", "episódio 5"), ou "última sessão"
    const numeros = [...q.matchAll(/(?:sessao|sessoes|episodio|episodios)\s*((?:\d+\s*(?:,|e|a|ate)?\s*)+)/g)]
      .flatMap((m) => {
        const n = m[1].match(/\d+/g).map(Number);
        return /\d+\s*(a|ate)\s*\d+/.test(m[1]) && n[1] - n[0] <= 10 ? Array.from({ length: n[1] - n[0] + 1 }, (_, i) => n[0] + i) : n;
      });
    const citadas = eps.filter((ep) => numeros.includes(numero(ep)));
    if (/(ultima|ultimo) (sessao|episodio)|(sessao|episodio) (passada|passado|anterior)/.test(q)) citadas.push(eps.at(-1));
    if (/penultim[ao] (sessao|episodio)/.test(q) && eps.length > 1) citadas.push(eps.at(-2));
    const final = /final|fim |fim$|termin|acab|encerr/.test(q), inicio = /inicio|comec|abertura/.test(q);
    for (const ep of new Set(citadas.filter(Boolean))) {
      partes.push(textoSessao(ep));
      const daSessao = falas.filter((f) => f.ep === ep);
      if (final) partes.push(`FINAL DA TRANSCRIÇÃO DA ${nomeSessao(ep).toUpperCase()}:\n` + daSessao.slice(-150).map(linkFala).join("\n"));
      if (inicio) partes.push(`INÍCIO DA TRANSCRIÇÃO DA ${nomeSessao(ep).toUpperCase()}:\n` + daSessao.slice(0, 150).map(linkFala).join("\n"));
      if (!final && !inicio && qt.length) {
        blocos.filter((b) => b.ep === ep).map((b) => [pontuar(qt, b.termos, blocos.length), b])
          .filter(([s]) => s > 0).sort((a, b) => b[0] - a[0]).slice(0, 4)
          .forEach(([, b]) => partes.push(`TRECHO DA ${nomeSessao(ep).toUpperCase()}:\n` + b.trecho.map(linkFala).join("\n")));
      }
      partes.push(transcricaoCompleta(ep));
    }

    // páginas cujo nome aparece na pergunta ("Oberon", "kobolds do Emaranhado")
    const qset = new Set(qt);
    const achadas = Object.entries(dados.entidades).map(([id, e]) => {
      let nota = 0;
      for (const nome of [e.nome, ...e.apelidos]) {
        if (nome.length >= 3 && frase(nome).test(q)) { nota = 1; break; }
        const t = termos(nome), comuns = t.filter((x) => qset.has(x));
        if (t.length && comuns.some((x) => x.length >= 5)) nota = Math.max(nota, comuns.length / t.length);
      }
      return [nota, id];
    }).filter(([n]) => n >= 0.5).sort((a, b) => b[0] - a[0]).slice(0, 5).map(([, id]) => id);
    for (const id of achadas) {
      const e = dados.entidades[id];
      partes.push(textoPagina(id), textoMencoes(e.nome, [e.nome, ...e.apelidos].filter((n) => n.length >= 3).map(frase)));
    }

    // nomes da pergunta que ainda não têm página: todas as falas que os citam ("Abicon", "kobolds")
    const cobertos = new Set(achadas.flatMap((id) => [dados.entidades[id].nome, ...dados.entidades[id].apelidos].flatMap(termos)));
    const total = blocos.length + docs.length;
    qt.filter((t) => !cobertos.has(t) && df.get(t) && df.get(t) <= total * 0.25)
      .sort((a, b) => df.get(a) - df.get(b)).slice(0, 3)
      .forEach((t) => partes.push(textoMencoes(t, [new RegExp(`(^|[^a-z0-9])${escapar(t)}`)])));

    // busca por palavras na wiki e nas transcrições
    if (qt.length) {
      docs.map((d) => [pontuar(qt, d.termos, docs.length), d]).filter(([s]) => s > 0)
        .sort((a, b) => b[0] - a[0]).slice(0, 5)
        .forEach(([, d]) => {
          if (d.tipo === "pagina" && !achadas.includes(d.id)) partes.push(textoPagina(d.id));
          if (d.tipo === "sessao" && !citadas.includes(d.id)) partes.push(textoSessao(d.id));
        });
      blocos.map((b) => [pontuar(qt, b.termos, blocos.length), b]).filter(([s]) => s > 0)
        .sort((a, b) => b[0] - a[0]).slice(0, 40)
        .forEach(([, b]) => partes.push(`TRECHO DA ${nomeSessao(b.ep).toUpperCase()}:\n` + b.trecho.map(linkFala).join("\n")));
    }

    const geral = [`LISTA DE SESSÕES:\n` + eps.map((ep) => `- ${linkSessao(ep)}`).join("\n")];
    if (dados.recapitulacao) geral.push(`RECAPITULAÇÃO MAIS RECENTE (depois da ${nomeSessao(dados.recapitulacao.apos)}):\n${dados.recapitulacao.texto}`);
    const abertas = dados.pendencias.filter((p) => !p.resolvida_em);
    if (abertas.length) geral.push("PENDÊNCIAS EM ABERTO (página: pendencias.html):\n" + abertas.map((p) => `- ${p.texto} (desde a ${nomeSessao(p.aberta_em)})`).join("\n"));
    if (Object.keys(dados.sessoes).length) geral.push("RESUMOS DE TODAS AS SESSÕES:\n" +
      Object.keys(dados.sessoes).sort(ordenar).map((ep) => `${linkSessao(ep)}: ${dados.sessoes[ep].resumo}`).join("\n"));
    const nomes = {};
    for (const [id, e] of Object.entries(dados.entidades)) (nomes[e.tipo] ||= []).push(`[${e.nome}](${id}.html)`);
    if (Object.keys(nomes).length) geral.push("TODAS AS PÁGINAS DA WIKI:\n" + Object.entries(nomes).map(([t, l]) => `${t}: ${l.join(", ")}`).join("\n"));

    let texto = "";
    for (const parte of [...geral.slice(0, 2), ...partes, ...geral.slice(2)]) {
      if (texto.length + parte.length > limite) { texto += "\n\n" + parte.slice(0, limite - texto.length); break; }
      texto += "\n\n" + parte;
    }
    return texto.trim();
  }

  const INSTRUCOES = `Você é o assistente da wiki de uma campanha de RPG de mesa. Responda em português do Brasil, de forma direta.
Use só as informações do CONTEXTO, que vem da wiki e das transcrições das sessões. Se a resposta não estiver no contexto, diga que não encontrou essa informação na wiki.
Nas transcrições, cada fala tem o horário, depois "Personagem (Jogador)" ou "Mestre". O Mestre narra e interpreta os NPCs.
Quando citar um momento, informe a sessão e a minutagem com link: use o link da fala ou, numa transcrição completa, monte o link pelo modelo indicado no cabeçalho dela.
Inclua links para as páginas da wiki relevantes, no formato [nome](link), usando exatamente os links do contexto. Não invente outros links.`;

  // ---------- conta OpenRouter ----------
  const base64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  async function conectar() {
    const verificador = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const desafio = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verificador)));
    sessionStorage.setItem("openrouter_verificador", verificador);
    location.href = `https://openrouter.ai/auth?callback_url=${encodeURIComponent(location.origin + location.pathname)}` +
      `&code_challenge=${desafio}&code_challenge_method=S256`;
  }

  async function concluirConexao() {
    const codigo = new URLSearchParams(location.search).get("code");
    const verificador = sessionStorage.getItem("openrouter_verificador");
    if (!codigo || !verificador) return;
    history.replaceState(null, "", location.pathname);
    sessionStorage.removeItem("openrouter_verificador");
    const r = await fetch("/api/chave", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: codigo, code_verifier: verificador, code_challenge_method: "S256" }) });
    const j = await r.json().catch(() => ({}));
    if (j.key) { chave = j.key; guardar("openrouter_chave", chave); } else mostrar("Não foi possível conectar ao OpenRouter. Tente de novo.", "erro");
  }

  async function montarModelos() {
    const sel = $("ia-modelo");
    sel.innerHTML = "";
    sel.append(new Option("Grátis do site (Gemini Flash-Lite)", "site"));
    $("ia-conectar").textContent = chave ? "Desconectar OpenRouter" : "Conectar conta OpenRouter";
    $("ia-conta").textContent = chave ? "Conectado ao OpenRouter" : "";
    if (chave) {
      try {
        const lista = ((await (await fetch("/api/modelos")).json()).data || [])
          .filter((m) => (m.context_length || 0) >= 32000 && (m.architecture?.output_modalities || ["text"]).includes("text"))
          .sort((a, b) => a.name.localeCompare(b.name));
        lista.forEach((m) => { contextos[m.id] = m.context_length; });
        const grupo = (rotulo, itens, texto) => {
          const g = document.createElement("optgroup");
          g.label = rotulo;
          itens.forEach((m) => g.append(new Option(texto(m), m.id)));
          if (itens.length) sel.append(g);
        };
        grupo("Gratuitos (cota da sua conta OpenRouter)", lista.filter((m) => m.id.endsWith(":free")), (m) => m.name);
        grupo("Pagos (cobrados nos seus créditos OpenRouter)", lista.filter((m) => !m.id.endsWith(":free") && Number(m.pricing?.prompt) > 0),
          (m) => `${m.name} · US$ ${(Number(m.pricing.prompt) * 1e6).toFixed(2)} por milhão de tokens de entrada`);
      } catch {
        mostrar("Não foi possível carregar a lista de modelos do OpenRouter.", "erro");
      }
    }
    sel.value = [...sel.options].some((o) => o.value === modelo) ? modelo : "site";
    modelo = sel.value;
  }

  // ---------- conversa ----------
  function mostrar(texto, classe, markdown = false) {
    const div = document.createElement("div");
    div.className = `msg ${classe}`;
    if (markdown && window.marked && window.DOMPurify) div.innerHTML = DOMPurify.sanitize(marked.parse(texto));
    else div.textContent = texto;
    $("ia-mensagens").append(div);
    div.scrollIntoView({ block: "nearest" });
    return div;
  }

  function erro(status, j, usandoConta) {
    const detalhe = j?.error?.message || j?.[0]?.error?.message || "";
    if (status === 429) return usandoConta ? "Limite de uso do modelo atingido na sua conta OpenRouter. Tente outro modelo ou mais tarde."
      : "O limite diário do modelo grátis do site acabou. Tente amanhã ou conecte sua conta OpenRouter.";
    if (usandoConta && (status === 401 || status === 403)) {
      chave = ""; guardar("openrouter_chave", ""); montarModelos();
      return "A conexão com o OpenRouter expirou. Conecte de novo.";
    }
    if (status === 402) return "Sua conta OpenRouter não tem créditos para este modelo.";
    return `Erro ${status} ao consultar o modelo. ${detalhe}`;
  }

  async function perguntar(pergunta) {
    mostrar(pergunta, "usuario");
    const espera = mostrar(dados ? "Pensando…" : "Carregando a wiki e as transcrições…", "ia");
    $("ia-enviar").disabled = true;
    try {
      await carregar();
      espera.textContent = "Pensando…";
      const usandoConta = modelo !== "site" && Boolean(chave);
      const limite = usandoConta ? Math.min(LIMITE_SITE, Math.floor((contextos[modelo] || 32000) * 2)) : LIMITE_SITE;
      const mensagens = [{ role: "system", content: `${INSTRUCOES}\n\nCONTEXTO:\n${contexto(pergunta, limite)}` },
        ...historico.slice(-6), { role: "user", content: pergunta }];
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(usandoConta ? { Authorization: `Bearer ${chave}` } : {}) },
        body: JSON.stringify({ modelo, mensagens }),
      });
      const j = await r.json().catch(() => ({}));
      espera.remove();
      if (!r.ok) { mostrar(erro(r.status, j, usandoConta), "erro"); return; }
      const resposta = j.choices?.[0]?.message?.content || "O modelo não devolveu resposta.";
      historico.push({ role: "user", content: pergunta }, { role: "assistant", content: resposta });
      mostrar(resposta, "ia", true);
    } catch (e) {
      espera.remove();
      mostrar(`Falha de conexão: ${e.message}`, "erro");
    } finally {
      $("ia-enviar").disabled = false;
    }
  }

  $("ia-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const pergunta = $("ia-pergunta").value.trim();
    if (!pergunta) return;
    $("ia-pergunta").value = "";
    perguntar(pergunta);
  });
  $("ia-pergunta").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("ia-form").requestSubmit(); }
  });
  $("ia-modelo").addEventListener("change", (ev) => { modelo = ev.target.value; guardar("modelo", modelo); });
  $("ia-conectar").addEventListener("click", () => {
    if (chave) { chave = ""; guardar("openrouter_chave", ""); modelo = "site"; montarModelos(); } else conectar();
  });

  concluirConexao().finally(montarModelos);
})();
