// Worker for cosmosdata.ai. Serves the static site and handles the contact form.
//
// POST /api/contact accepts JSON (from the page script) or a classic form post (without
// JavaScript), validates it, and mails it to CONTACT_TO through the Email Routing binding,
// with the visitor's address as Reply-To. Spam guards: a hidden honeypot field, a minimum
// time on the page, a same-origin check and size limits.

import { EmailMessage } from "cloudflare:email";

const LIMITS = { name: 120, email: 254, company: 120, message: 5000 };
const MIN_SECONDS_ON_PAGE = 3;
const EMAIL_RE = /^[^\s@<>"]+@[^\s@<>"]+\.[^\s@<>"]+$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname === "www.cosmosdata.ai") {
      url.hostname = "cosmosdata.ai";
      return Response.redirect(url.toString(), 301);
    }
    if (url.pathname === "/api/contact") {
      if (request.method !== "POST") {
        return json({ ok: false, error: "Use POST." }, 405, { Allow: "POST" });
      }
      return handleContact(request, env);
    }
    return env.ASSETS.fetch(request);
  },
};

async function handleContact(request, env) {
  const wantsJson = (request.headers.get("Content-Type") || "").includes("application/json");
  const reply = (status, ok, error) =>
    wantsJson
      ? json(ok ? { ok: true } : { ok: false, error }, status)
      : Response.redirect(new URL(`/?contact=${ok ? "sent" : "error"}#contact`, request.url).toString(), 303);

  // Only the site's own pages may post here (browsers always send Origin on POST).
  const origin = request.headers.get("Origin");
  if (origin && origin !== new URL(request.url).origin) {
    return reply(403, false, "Unknown origin.");
  }

  let data;
  try {
    data = wantsJson ? await request.json() : Object.fromEntries(await request.formData());
  } catch {
    return reply(400, false, "Could not read the form.");
  }

  // Bots fill the hidden field or submit instantly; pretend it worked and drop it.
  const startedAt = Number(data.started_at) || 0;
  if (data.website || (startedAt && Date.now() - startedAt < MIN_SECONDS_ON_PAGE * 1000)) {
    return reply(200, true);
  }

  const form = {};
  for (const field of Object.keys(LIMITS)) {
    form[field] = String(data[field] ?? "").trim();
  }
  if (!form.name || !form.email || !form.message) {
    return reply(400, false, "Name, email and message are required.");
  }
  if (!EMAIL_RE.test(form.email)) {
    return reply(400, false, "That email address does not look valid.");
  }
  for (const [field, max] of Object.entries(LIMITS)) {
    if (form[field].length > max) {
      return reply(400, false, `The ${field} field is too long (max ${max} characters).`);
    }
  }

  try {
    const raw = buildMessage(env, form, request);
    await env.CONTACT_EMAIL.send(new EmailMessage(env.CONTACT_FROM, env.CONTACT_TO, raw));
  } catch (error) {
    console.error("contact form: send failed", error);
    return reply(502, false, "The message could not be sent. Please write to administrator@cosmosdata.ai.");
  }
  return reply(200, true);
}

function buildMessage(env, form, request) {
  const oneLine = (value) => value.replace(/[\r\n]+/g, " ");
  const country = request.cf?.country || "unknown";
  const body = [
    `Name: ${form.name}`,
    `Email: ${form.email}`,
    `Company: ${form.company || "-"}`,
    `Country: ${country}`,
    "",
    form.message,
    "",
    "-- ",
    "Sent from the contact form on https://cosmosdata.ai",
  ].join("\r\n");

  const domain = env.CONTACT_FROM.split("@")[1];
  const headers = [
    `From: "Cosmos Semantic website" <${env.CONTACT_FROM}>`,
    `To: <${env.CONTACT_TO}>`,
    `Reply-To: ${encodeWord(oneLine(form.name))} <${oneLine(form.email)}>`,
    `Subject: ${encodeWord(`Contact: ${oneLine(form.name)}${form.company ? ` (${oneLine(form.company)})` : ""}`)}`,
    `Message-ID: <${crypto.randomUUID()}@${domain}>`,
    `Date: ${new Date().toUTCString()}`,
    "MIME-Version: 1.0",
    'Content-Type: text/plain; charset="utf-8"',
    "Content-Transfer-Encoding: base64",
  ];
  return `${headers.join("\r\n")}\r\n\r\n${wrap(base64(body))}\r\n`;
}

// RFC 2047 encoded word, so names with accents survive in headers.
function encodeWord(text) {
  return `=?UTF-8?B?${base64(text)}?=`;
}

function base64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function wrap(text) {
  return text.match(/.{1,76}/g)?.join("\r\n") ?? "";
}

function json(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers },
  });
}
