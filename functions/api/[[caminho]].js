// Intermediário do chat de IA, roda no Cloudflare Pages.
// /api/chat     envia a conversa ao Gemini gratuito do site, ou ao OpenRouter quando o visitante conectou a conta dele
// /api/modelos  lista os modelos do OpenRouter
// /api/chave    troca o código de login do OpenRouter pela chave do visitante
const GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions";
const OPENROUTER = "https://openrouter.ai/api/v1";
const MODELO_GRATIS = "gemini-2.5-flash-lite";

export async function onRequest({ request, env }) {
  const url = new URL(request.url);
  const origem = request.headers.get("Origin");
  if (origem && origem !== url.origin) return resposta(403, "Uso permitido só pela própria wiki");
  const rota = url.pathname.replace(/^\/api\//, "");

  if (rota === "modelos") return repassar(fetch(`${OPENROUTER}/models`));
  if (request.method !== "POST") return resposta(405, "Use POST");
  const corpo = await request.text();
  if (corpo.length > 1_000_000) return resposta(413, "Pergunta grande demais");

  if (rota === "chave") {
    return repassar(fetch(`${OPENROUTER}/auth/keys`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: corpo }));
  }
  if (rota === "chat") {
    const { modelo, mensagens } = JSON.parse(corpo);
    const chaveVisitante = request.headers.get("Authorization");
    if (chaveVisitante) { // modelo escolhido pelo visitante, cobrado na conta OpenRouter dele
      return repassar(fetch(`${OPENROUTER}/chat/completions`, {
        method: "POST",
        headers: { Authorization: chaveVisitante, "Content-Type": "application/json",
                   "HTTP-Referer": url.origin, "X-Title": "Wiki da campanha" },
        body: JSON.stringify({ model: modelo, messages: mensagens }) }));
    }
    if (!env.GEMINI_API_KEY) return resposta(500, "Chave GEMINI_API_KEY não configurada no Cloudflare");
    return repassar(fetch(GEMINI, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.GEMINI_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: MODELO_GRATIS, messages: mensagens }) }));
  }
  return resposta(404, "Rota desconhecida");
}

async function repassar(pedido) {
  const r = await pedido;
  return new Response(r.body, { status: r.status, headers: { "Content-Type": r.headers.get("Content-Type") || "application/json" } });
}

function resposta(status, mensagem) {
  return new Response(JSON.stringify({ error: { message: mensagem } }), { status, headers: { "Content-Type": "application/json" } });
}
