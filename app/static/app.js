const $ = (id) => document.getElementById(id);
const DAY_START = 0, DAY_END = 24 * 60;        // o dia inteiro, de 00:00 a 00:00
const MIN_GAP = 30;                             // buracos menores que isso não viram linha "livre"
const MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];

let vncUrl = "";
fetch("/api/config").then((r) => r.json()).then((c) => {
  vncUrl = `${location.protocol}//${location.hostname}:${c.novnc_port}/vnc.html?autoconnect=1&resize=scale`;
});

let current = isoDate(new Date());
const VIEWS = ["day", "week", "todo", "accounts"];
let view = VIEWS.includes(localStorage.getItem("agenda.view")) ? localStorage.getItem("agenda.view") : "day";
let tdata = null;      // to-do
let adata = null;      // contas
let data = null;       // dia aberto
let wdata = null;      // semana aberta
let refreshing = false;
let pendingFocus = null;   // reunião a destacar quando o dia abrir (vindo da semana)
let showSameClashes = false;

function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function shiftDay(iso, n) {
  const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + n); return isoDate(d);
}
function toMin(hhmm) {
  if (!hhmm) return null;
  const [h, m] = hhmm.split(":").map(Number); return h * 60 + m;
}
// 24:00 aparece como 00:00 (fim do dia)
function fmtMin(min) { const m = min % 1440; return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; }
function fmtDur(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? (m ? `${h}h${String(m).padStart(2, "0")}` : `${h}h`) : `${m} min`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(u) {
  try { const url = new URL(u); return url.protocol === "https:" ? url.href : null; } catch { return null; }
}
function nowMinutes() { const n = new Date(); return n.getHours() * 60 + n.getMinutes(); }
function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
function seps(html) { return html.replace(/ · /g, '<span class="sep" aria-hidden="true">·</span>'); }
function setHTML(id, html) { $(id).innerHTML = seps(html); }

// Texto do selo em preto ou branco, o que tiver mais contraste com a cor da empresa
function onColor(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "#fff";
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return (lum + 0.05) / 0.0613 >= 1.05 / (lum + 0.05) ? "#111729" : "#fff";   // 0.0613 = luminância do texto escuro + 0,05
}
function badge(a, sm = false) {
  return `<span class="ln${sm ? " sm" : ""}" style="--c:${esc(a.color)}" title="${esc(a.name)}"><i></i>${esc(a.short || a.name.slice(0, 3).toUpperCase())}</span>`;
}

// Mensagens técnicas viram texto com o que fazer; o detalhe fica no title
function accountStatus(a) {
  const when = a.updated_at ? a.updated_at.slice(11, 16) : null;
  if (a.status === "ok" && a.missing) return { bad: true, retry: true,
    text: `O calendário tem ${plural(a.missing, "evento", "eventos")} que não consegui ler. As demais reuniões estão na lista.` };
  if (a.status === "ok") return { bad: false, text: when ? `<span class="long-only">atualizado </span>${when}` : "atualizado", html: true };
  if (a.status === "pending") return { bad: false, text: "aguardando a primeira leitura" };
  if (a.status === "login_required") return { bad: true, login: true,
    text: "Precisa de login. Entre pelo navegador das contas (a senha é a VNC_PASSWORD do .env)." };
  const err = a.error || "";
  let text = "Não consegui ler o calendário desta conta.";
  if (/closed|TargetClosed/i.test(err)) text = "O navegador desta conta foi fechado. Ele reabre sozinho ao atualizar.";
  else if (err === "calendar_no_data") text = "O calendário abriu, mas não enviou as reuniões deste dia.";
  else if (/não carregou|Timeout/i.test(err)) text = "O calendário demorou demais para abrir.";
  if (a.stale && when) text += ` Mostrando as reuniões lidas às ${when}.`;
  return { bad: true, retry: true, text };
}

function setBusy(on) {
  refreshing = on;
  $("refresh").classList.toggle("busy", on);
  $("refresh").disabled = on;
  $("refresh-label").textContent = on ? "Atualizando…" : "Atualizar";
}

async function load({ method = "GET", quiet = false, span = "week" } = {}) {
  if (view === "todo" && method === "GET") return loadTodo();
  if (view === "accounts" && method === "GET") return loadAccounts();
  if (refreshing && method === "POST") return;
  if (method === "POST") setBusy(true);
  if (!quiet && !data && view === "day") setHTML("events", '<li class="skel"></li><li class="skel"></li><li class="skel"></li>');
  const asked = current, askedView = view;
  // Dia ou semana fora do cache leva ~15-30 s (o agente lê as contas na hora): mostra o destino já
  const slow = method === "GET" && !quiet ? setTimeout(() => showLoading(asked, askedView), 350) : null;
  try {
    const url = method === "POST" ? `/api/refresh?date=${asked}&span=${span}`
      : `/api/${askedView === "week" ? "week" : "agenda"}?date=${asked}`;
    const res = await fetch(url, { method });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (asked !== current || askedView !== view) return;   // o usuário já mudou de dia ou de visão
    hideLoading();
    if (method === "POST") { data = body.day; wdata = body.week; }
    else if (askedView === "week") wdata = body;
    else data = body;
    $("alert").innerHTML = "";
    render();
  } catch (e) {
    if (!quiet) $("alert").innerHTML = `<div class="alert">O serviço da agenda não respondeu. Confira se o container está rodando (<code>docker compose ps</code>) e tente de novo.</div>`;
  } finally {
    clearTimeout(slow);
    if (asked === current && askedView === view) hideLoading();
    if (method === "POST") setBusy(false);
  }
}

function showLoading(iso, v) {
  $("main").classList.add("loading");
  $("main").setAttribute("aria-busy", "true");
  if (v === "week") {
    const start = shiftDay(iso, -new Date(iso + "T12:00:00").getDay()), end = shiftDay(start, 6);
    $("title").textContent = weekTitle(start);
    $("date").textContent = rangeLabel(start, end);
  } else {
    $("title").textContent = dayTitle(iso);
    $("date").textContent = new Date(iso + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }
  $("summary").textContent = "Lendo as agendas… na primeira vez leva até 30 segundos.";
}

function hideLoading() {
  $("main").classList.remove("loading");
  $("main").removeAttribute("aria-busy");
}

function dayTitle(iso) {
  const today = isoDate(new Date());
  if (iso === today) return "Hoje";
  if (iso === shiftDay(today, 1)) return "Amanhã";
  if (iso === shiftDay(today, -1)) return "Ontem";
  const w = new Date(iso + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "long" });
  return w[0].toUpperCase() + w.slice(1);
}

function weekTitle(startIso) {
  const today = isoDate(new Date());
  const thisStart = shiftDay(today, -new Date(today + "T12:00:00").getDay());
  const n = Math.round((new Date(startIso + "T12:00:00") - new Date(thisStart + "T12:00:00")) / (7 * 864e5));
  if (n === 0) return "Esta semana";
  if (n === 1) return "Próxima semana";
  if (n === -1) return "Semana passada";
  return n > 0 ? `Daqui a ${n} semanas` : `Há ${-n} semanas`;
}

function rangeLabel(startIso, endIso) {
  const a = new Date(startIso + "T12:00:00"), b = new Date(endIso + "T12:00:00");
  const sameMonth = a.getMonth() === b.getMonth();
  return `${a.getDate()}${sameMonth ? "" : ` de ${MONTHS[a.getMonth()]}`} a ${b.getDate()} de ${MONTHS[b.getMonth()]} de ${b.getFullYear()}`;
}

const ME = { id: "eu", name: "Você", short: "EU", color: "#16a394" };

function prepare(accs, plan = {}) {
  const skipped = new Set(plan.skipped || []);
  const events = accs.flatMap((a, lane) => a.events.map((e) => ({ ...e, account: a, lane })));
  events.forEach((e) => { e.skipped = skipped.has(evKey(e)); });
  const allDay = events.filter((e) => e.all_day || !e.start);
  const timed = events.filter((e) => !e.all_day && e.start).map((e) => {
    const s = toMin(e.start), en = toMin(e.end);
    return { ...e, s, e2: en != null && en > s ? en : s + 30 };
  }).sort((x, y) => x.s - y.s || x.e2 - y.e2);

  // "Não vou" conta como recusada: sai de conflitos, horas e tempo ocupado
  const active = timed.filter((e) => e.response !== "declined" && !e.skipped);
  const pairs = [];
  active.forEach((e, i) => {
    e.clashWith = active.filter((o) => o !== e && o.s < e.e2 && o.e2 > e.s);
    // Entre empresas exige escolher uma reunião; dentro da mesma empresa costuma ser sobreposição combinada
    e.cross = e.clashWith.some((o) => o.account.id !== e.account.id);
    active.slice(i + 1).forEach((o) => { if (o.s < e.e2 && o.e2 > e.s) pairs.push({ a: e, b: o, cross: o.account.id !== e.account.id }); });
  });
  // Tempo ocupado = união dos intervalos (sobreposições não contam duas vezes)
  let busy = 0, cur = null;
  for (const e of active) {
    if (!cur || e.s > cur[1]) { if (cur) busy += cur[1] - cur[0]; cur = [e.s, e.e2]; }
    else cur[1] = Math.max(cur[1], e.e2);
  }
  if (cur) busy += cur[1] - cur[0];
  timed.forEach((e, i) => { e.id = `ev-${i}`; });
  const acts = (plan.activities || []).map((a) => ({ ...a, s: toMin(a.start), e2: toMin(a.end), mine: true }));
  return { allDay, timed, active, pairs, busy, acts };
}

function clashSummary(pairs) {
  const cross = pairs.filter((p) => p.cross).length, same = pairs.length - cross;
  return [
    cross ? `<span class="warn">${plural(cross, "conflito", "conflitos")} entre empresas</span>` : null,
    same ? plural(same, "sobreposição", "sobreposições") : null,
  ].filter(Boolean);
}

function render() {
  const cal = view === "day" || view === "week";
  $("dayview").hidden = view !== "day";
  $("weekview").hidden = view !== "week";
  $("todoview").hidden = view !== "todo";
  $("accountsview").hidden = view !== "accounts";
  $("nav").style.visibility = cal ? "" : "hidden";   // some sem mudar o lugar das abas
  $("accounts").hidden = !cal;
  $("main").classList.add("wide");
  VIEWS.forEach((v) => $(`view-${v}`).setAttribute("aria-pressed", String(view === v)));
  $("prev").setAttribute("aria-label", view === "week" ? "Semana anterior" : "Dia anterior");
  $("next").setAttribute("aria-label", view === "week" ? "Próxima semana" : "Próximo dia");
  if (view === "week") { if (wdata) renderWeek(); return; }
  if (view === "todo") { renderTodo(); return; }
  if (view === "accounts") { renderAccountsView(); return; }
  if (data) renderDay();
}

function renderDay() {
  const day = new Date(data.date + "T12:00:00");
  const rel = ["Hoje", "Amanhã", "Ontem"].includes(dayTitle(data.date));
  const weekday = day.toLocaleDateString("pt-BR", { weekday: "long" });
  $("title").textContent = dayTitle(data.date);
  $("date").textContent = (rel ? weekday + ", " : "") + day.toLocaleDateString("pt-BR", { day: "numeric", month: "long", year: "numeric" });
  setTitle(`${$("title").textContent} · ManaTime`);

  const isToday = data.date === isoDate(new Date());
  const nowMin = isToday ? nowMinutes() : null;
  const prep = prepare(data.accounts, data.plan);

  renderAccounts(data.accounts);
  renderSummary(prep.active, prep.pairs, prep.busy, nowMin);
  renderRibbon(prep.timed, prep.acts, nowMin);
  renderList(prep, nowMin);
}

function renderAccounts(accs) {
  $("accounts").innerHTML = accs.map((a) => {
    const st = accountStatus(a);
    const action = st.login ? `<button data-login="${esc(a.id)}">Fazer login</button>`
      : st.retry ? `<button data-retry>Tentar de novo</button>` : "";
    return `<li class="acc${st.bad ? " bad" : ""}" title="${esc(a.error || "")}">
      <span class="dot" style="--c:${esc(a.color)}" aria-hidden="true"></span><span class="name">${esc(a.name)}</span>
      <span class="st num">${st.html ? st.text : esc(st.text)}</span>${action}
    </li>`;
  }).join("");
}

function renderSummary(active, pairs, busy, nowMin) {
  if (!active.length) { $("summary").textContent = ""; return; }   // a lista já diz "Dia livre"
  const parts = [`<b>${plural(active.length, "reunião", "reuniões")}</b>`, `${fmtDur(busy)} ocupado`, ...clashSummary(pairs)];
  if (nowMin != null) {
    const live = active.find((e) => e.s <= nowMin && nowMin < e.e2);
    const next = active.find((e) => e.s > nowMin);
    if (live) parts.push(`agora: ${esc(live.title.length > 48 ? live.title.slice(0, 46).trimEnd() + "…" : live.title)}`);
    else if (next) parts.push(`próxima em <b>${fmtDur(next.s - nowMin)}</b>`);
  }
  setHTML("summary", parts.join(" · "));
}

function renderRibbon(timed, acts, nowMin) {
  if (!timed.length && !acts.length) { $("ribbon").hidden = true; return; }
  const all = [...timed, ...acts];
  // Hoje, a faixa sempre inclui o horário atual (cedo ou tarde fora das 7h–20h)
  const marks = nowMin != null ? [...all.map((e) => e.s), nowMin] : all.map((e) => e.s);
  const first = Math.min(DAY_START, Math.floor(Math.min(...marks) / 60) * 60);
  const last = Math.max(DAY_END, Math.ceil(Math.max(...all.map((e) => e.e2), ...(nowMin != null ? [nowMin + 1] : [])) / 60) * 60);
  const span = last - first;
  const pct = (m) => `${((m - first) / span) * 100}%`;
  const step = innerWidth < 700 ? 240 : 120;   // rótulos a cada 2 h (4 h no celular)

  const lanes = acts.length ? [...data.accounts, ME] : data.accounts;
  const names = lanes.map((a) => `<div class="lane-name">${badge(a, true)}</div>`).join("");
  const tracks = data.accounts.map((a, i) => {
    const blocks = timed.filter((e) => e.lane === i).map((e) => {
      const cls = ["blk", e.skipped ? "declined" : e.response, e.cross ? "cross" : ""].join(" ");
      return `<div class="${cls}" data-goto="${e.id}" title="${esc(`${fmtMin(e.s)}–${fmtMin(e.e2)} ${e.title}`)}"
        style="left:calc(${pct(e.s)} + 1px);width:calc(${pct(e.e2)} - ${pct(e.s)} - 2px)"></div>`;
    }).join("");
    return `<div class="track" style="--c:${esc(a.color)}">${blocks}</div>`;
  }).join("") + (acts.length ? `<div class="track" style="--c:${ME.color}">${acts.map((x) =>
    `<div class="blk mine" data-goto="act-${esc(x.id)}" title="${esc(`${x.start}–${x.end} ${x.title}`)}"
      style="left:calc(${pct(x.s)} + 1px);width:calc(${pct(x.e2)} - ${pct(x.s)} - 2px)"></div>`).join("")}</div>` : "");
  const ticks = [];
  for (let m = Math.ceil(first / step) * step; m <= last; m += step) ticks.push(`<span class="num" style="left:${pct(m)}">${(m / 60) % 24}h</span>`);
  const now = nowMin != null && nowMin >= first && nowMin <= last ? `<div class="nowline" style="left:${pct(nowMin)}"></div>` : "";
  // Uma linha fina por hora, atrás dos blocos: dá para ler 10:30 sem estimar
  const grid = [];
  for (let m = Math.ceil(first / 60) * 60 + 60; m < last; m += 60) grid.push(`<i class="hourline${m % step ? "" : " major"}" style="left:${pct(m)}"></i>`);

  $("ribbon").innerHTML = `<div class="lanes"><div class="names">${names}</div><div class="tracks">${tracks}${grid.join("")}${now}</div></div>
    <div class="axis">${ticks.join("")}</div>
    <div class="legend"><span><i class="key solid"></i>confirmada</span><span><i class="key open"></i>talvez ou sem resposta</span><span><i class="key cross"></i>conflito entre empresas</span></div>`;
  $("ribbon").hidden = false;
}

let formGap = null;   // tempo livre com o formulário de atividade aberto: "início-fim"

function freeSlots(busyItems, from, to) {
  const spans = busyItems.map((x) => [Math.max(x.s, from), Math.min(x.e2, to)]).filter(([a, b]) => b > a).sort((a, b) => a[0] - b[0]);
  const slots = [];
  let t = from;
  for (const [a, b] of spans) { if (a - t >= MIN_GAP) slots.push([t, a]); t = Math.max(t, b); }
  if (to - t >= MIN_GAP) slots.push([t, to]);
  return slots;
}

function renderList(prep, nowMin) {
  const { allDay, timed, active, acts } = prep;
  // Tempo livre: das 7h às 20h (ou do horário atual, se for hoje), entre reuniões e atividades
  const from = nowMin != null ? Math.max(DAY_START, Math.ceil(nowMin / 15) * 15) : DAY_START;
  const to = Math.max(DAY_END, ...active.map((e) => e.e2), ...acts.map((a) => a.e2));
  const slots = from < to ? freeSlots([...active, ...acts], from, to) : [];

  const entries = [
    ...timed.map((e) => ({ t: e.s, k: 2, html: row(e, null, nowMin != null && e.e2 <= nowMin, nowMin != null && e.s <= nowMin && nowMin < e.e2) })),
    ...acts.map((a) => ({ t: a.s, k: 2, html: actRow(a, nowMin != null && a.e2 <= nowMin) })),
    // tarefas com horário deste dia aparecem no trilho como pontos (não ocupam tempo livre)
    ...(data.plan?.tasks || []).filter((t) => t.time).map((t) => ({ t: toMin(t.time), k: 2, html: taskRow(t, nowMin) })),
    ...slots.map(([a, b]) => ({ t: a, k: 1, html: gapRow(a, b) })),
  ];
  if (nowMin != null) entries.push({ t: nowMin, k: 0, html: nowRow(nowMin) });
  entries.sort((x, y) => x.t - y.t || x.k - y.k);
  const rows = [...allDay.map((e) => row(e, "Dia todo", false)), ...entries.map((x) => x.html)];

  const synced = data.accounts.some((a) => a.status === "ok" || a.stale);
  const head = !data.accounts.length ? `<li class="empty"><b>Nenhuma conta ainda</b>Adicione suas agendas na aba Contas.</li>`
    : synced ? "" : `<li class="empty"><b>Nenhuma conta sincronizada ainda</b>Use “Fazer login” nas contas acima e depois atualize.</li>`;
  setHTML("events", head + rows.join(""));
  focusPending();
  const form = $("act-form");
  if (form) form.querySelector("input[name=title]").focus();
}

function gapRow(a, b) {
  const key = `${fmtMin(a)}-${fmtMin(b)}`;
  if (formGap === key) {
    const end = Math.min(b, a + 60);
    return `<li class="actform"><span></span><span class="rail" aria-hidden="true"></span><form id="act-form" data-gap="${key}">
      <input name="title" maxlength="200" placeholder="O que você vai fazer?" aria-label="Atividade" required>
      <input name="start" type="text" inputmode="numeric" maxlength="5" autocomplete="off" class="hhmm" value="${fmtMin(a)}" aria-label="Início" required>
      <span aria-hidden="true">até</span>
      <input name="end" type="text" inputmode="numeric" maxlength="5" autocomplete="off" class="hhmm" value="${fmtMin(end)}" aria-label="Fim" required>
      <button type="submit" class="primary">Adicionar</button><button type="button" data-cancelact>Cancelar</button>
      <p class="formerr" id="act-err" role="alert"></p></form></li>`;
  }
  return `<li class="gap num"><span></span><span class="rail" aria-hidden="true"></span>
    <div class="free"><span><b>${fmtDur(b - a)} livre</b> · ${fmtMin(a)}–${fmtMin(b)}</span>
    <button class="addbtn" data-addact="${key}" aria-label="Adicionar atividade entre ${fmtMin(a)} e ${fmtMin(b)}"><svg class="i"><use href="#i-plus"/></svg>atividade</button></div></li>`;
}

function actRow(a, past) {
  return `<li class="ev mine${past ? " past" : ""}" id="act-${esc(a.id)}" style="--c:${ME.color}">
    <div class="time num">${a.start}<span class="end">${a.end}</span></div>
    <span class="rail" aria-hidden="true"><i class="node"></i></span>
    <div><div class="title">${esc(a.title)}</div><div class="meta"><span class="co">Você</span><span>atividade</span></div></div>
    <div class="actions"><button class="ghost" data-delact="${esc(a.id)}" aria-label="Remover atividade ${esc(a.title)}" title="Remover"><svg class="i"><use href="#i-x"/></svg></button></div>
  </li>`;
}

function taskRow(t, nowMin) {
  const late = nowMin != null && !t.done && toMin(t.time) < nowMin;
  return `<li class="ev task-ev${t.done ? " done" : ""}" style="--c:${ME.color}">
    <div class="time num">${t.time}</div>
    <span class="rail" aria-hidden="true"><i class="node"></i></span>
    <div><label class="title" for="dt-${esc(t.id)}">${esc(t.title)}</label>
      <div class="meta"><span class="co">Tarefa</span>${(t.tags || []).map((g) => `<span>#${esc(g)}</span>`).join("")}${late ? `<span class="late">atrasada</span>` : ""}</div></div>
    <div class="actions"><input type="checkbox" id="dt-${esc(t.id)}" data-todo="${esc(t.id)}"${t.done ? " checked" : ""} aria-label="Concluir ${esc(t.title)}"></div>
  </li>`;
}

function focusPending() {
  if (!pendingFocus) return;
  const el = document.querySelector(`#events [data-k="${CSS.escape(pendingFocus)}"]`);
  pendingFocus = null;
  if (!el) return;
  el.scrollIntoView({ block: "center" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}

function evKey(e) { return `${e.account.id}|${e.start || "dia"}|${e.title}`; }

function nowRow(nowMin) {
  return `<li class="nowrow num" aria-label="Agora, ${fmtMin(nowMin)}"><span class="time">${fmtMin(nowMin)}</span>
    <span class="rail" aria-hidden="true"><i class="pulse"></i></span><span class="lbl">agora</span></li>`;
}

const TEAMS_LOCATION = /^(reuni(ão|ões) do microsoft teams|microsoft teams meeting)$/i;

// Conflito: escolher esta marca as outras do mesmo horário como "não vou" (só no dashboard)
function actions(e, past) {
  if (past || e.start == null) return "";
  if (e.skipped) return `<div class="actions"><button class="ghost" data-unskip="${esc(evKey(e))}">Desfazer</button></div>`;
  if (!e.clashWith?.length) return "";
  const others = e.clashWith.map(evKey);
  return `<div class="actions"><button class="choose" data-choose="${esc(evKey(e))}" data-others="${esc(JSON.stringify(others))}"
    title="Fica esta; ${plural(others.length, "a outra sai", "as outras saem")} do seu dia">Vou nesta</button></div>`;
}

function row(e, timeLabel, past, live) {
  const declined = e.response === "declined" || e.skipped;
  const cls = ["ev", declined ? "declined" : e.response, past && "past", live && "now", e.skipped && "skipped",
               e.cross && !past && "cross", timeLabel && "allday"].filter(Boolean).join(" ");
  const time = timeLabel ? `<span>${timeLabel}</span>` : `${e.start}<span class="end">${e.end || ""}</span>`;
  const crossNames = [...new Set((e.clashWith || []).filter((o) => o.account.id !== e.account.id).map((o) => o.account.name))];
  const sameTimes = [...new Set((e.clashWith || []).filter((o) => o.account.id === e.account.id).map((o) => o.start))];
  const tags = [
    crossNames.length && `<span class="tag warn">conflito com ${esc(crossNames.join(", "))}</span>`,
    e.skipped && `<span class="tag">você não vai</span>`,
    e.response === "declined" && `<span class="tag">recusada</span>`,
  ].filter(Boolean).join("");
  const place = e.location && TEAMS_LOCATION.test(e.location) ? "Teams" : e.location;
  const meta = [
    `<span class="co">${esc(e.account.name)}</span>`,
    e.organizer && `<span>${esc(e.organizer)}</span>`,
    place && !safeUrl(place) && `<span>${esc(place)}</span>`,
    e.response === "tentative" && `<span class="soft">talvez</span>`,
    e.response === "unknown" && e.account.provider !== "google" && `<span class="soft">sem resposta</span>`,
    sameTimes.length && `<span class="soft" title="Outra reunião da mesma empresa no mesmo horário">sobrepõe ${esc(sameTimes.join(", "))}</span>`,
  ].filter(Boolean).join("");
  return `<li class="${cls}" id="${e.id || ""}" data-k="${esc(evKey(e))}" style="--c:${esc(e.account.color)}">
    <div class="time num">${time}</div>
    <span class="rail" aria-hidden="true"><i class="node"></i></span>
    <div><div class="title">${esc(e.title)}${tags ? `<span class="tags">${tags}</span>` : ""}</div><div class="meta">${meta}</div></div>
    ${actions(e, past)}
  </li>`;
}

// "9" -> 09:00, "930" -> 09:30, "9:5" -> 09:05, "14h" -> 14:00; inválido -> null
function parseHHMM(v) {
  const t = String(v || "").trim().toLowerCase().replace("h", ":");
  if (!t) return "";
  let m = /^(\d{1,2})(?::(\d{1,2}))?:?$/.exec(t) || /^(\d{1,2})(\d{2})$/.exec(t);
  if (!m) return null;
  const h = Number(m[1]), mi = Number(m[2] || 0);
  return h < 24 && mi < 60 ? `${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}` : null;
}
document.addEventListener("focusout", (ev) => {
  if (!ev.target.matches?.("input.hhmm")) return;
  const v = parseHHMM(ev.target.value);
  if (v) ev.target.value = v;
});

async function api(method, url, body) {
  const res = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let msg = "Não consegui salvar. Tente de novo.";
    try {
      const j = await res.json();
      if (typeof j.detail === "string") msg = j.detail;
      else if (j.detail?.[0]?.msg) msg = j.detail[0].msg.replace(/^Value error, /, "");
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

// Depois de mudar o plano, relê só o cache (rápido) e redesenha
async function afterPlanChange() { await load({ quiet: true }); }

document.addEventListener("submit", async (ev) => {
  if (ev.target.id !== "act-form") return;
  ev.preventDefault();
  const f = ev.target, title = f.title.value.trim(), start = parseHHMM(f.start.value), end = parseHHMM(f.end.value);
  const err = (m) => { $("act-err").textContent = m; };
  if (!title) return err("Dê um nome para a atividade.");
  if (!start || !end) return err("Horário inválido. Use o formato 14:30.");
  if (!start || !end || end <= start) return err("O fim precisa ser depois do início.");
  // Atividade é para o tempo livre: avisa se cruzar uma reunião que você vai
  const s = toMin(start), e2 = toMin(end);
  const hit = prepare(data.accounts, data.plan).active.find((m) => m.s < e2 && m.e2 > s);
  if (hit) return err(`Esse horário cruza com “${hit.title}” (${hit.start}–${hit.end}).`);
  try {
    await api("POST", "/api/plan/activities", { date: data.date, start, end, title });
    formGap = null;
    await afterPlanChange();
  } catch (e) { err(e.message); }
});

// ---------- Semana ----------

function weekStatus(w) {
  // Pior situação de cada conta na semana: é o que precisa de atenção
  const rank = { login_required: 4, error: 3, pending: 1, ok: 0 };
  return w.days[0].accounts.map((a, i) => {
    let worst = null;
    for (const d of w.days) {
      const x = d.accounts[i];
      const r = (rank[x.status] ?? 2) + (x.missing ? 2.5 : 0);
      if (!worst || r > worst.r || (r === worst.r && (x.updated_at || "") > (worst.x.updated_at || ""))) worst = { r, x };
    }
    return worst.x;
  });
}

function renderWeek() {
  const w = wdata;
  $("title").textContent = weekTitle(w.start);
  $("date").textContent = rangeLabel(w.start, w.end);
  setTitle(`${$("title").textContent} · ManaTime`);

  const days = w.days.map((d) => ({ d, p: prepare(d.accounts, d.plan) }));
  const meetings = days.reduce((n, x) => n + x.p.active.length, 0);
  const busy = days.reduce((n, x) => n + x.p.busy, 0);
  const pairs = days.flatMap(({ d, p }) => p.pairs.map((x) => ({ ...x, date: d.date })));
  setHTML("summary", !meetings ? "" : [`<b>${plural(meetings, "reunião", "reuniões")}</b>`, `${fmtDur(busy)} ocupado`, ...clashSummary(pairs)].join(" · "));

  renderAccounts(weekStatus(w));
  renderHours(w, busy);
  renderClashes(pairs);
  renderGrid(days);
}

function renderHours(w, weekBusy) {
  const accs = w.days[0].accounts;
  const wk = w.totals.week.accounts, mo = w.totals.month;
  const max = Math.max(60, ...accs.map((a) => wk[a.id].minutes));
  const monthName = MONTHS[Number(w.month.slice(5)) - 1];
  const rows = accs.map((a) => {
    const x = wk[a.id], m = mo.accounts[a.id];
    return `<div class="who">${badge(a)}<span>${esc(a.name)}</span></div>
      <div class="bar num" style="--c:${esc(a.color)}">${x.minutes ? `<i style="width:${(x.minutes / max) * 100}%"></i>` : ""}
        <span><span class="short-only">semana </span>${x.minutes ? `<b>${fmtDur(x.minutes)}</b> · ${x.meetings} reun.` : "—"}</span></div>
      <div class="month num"><span class="short-only">${monthName} </span>${m.minutes ? `<b>${fmtDur(m.minutes)}</b> · ${m.meetings} reun.` : "—"}</div>`;
  }).join("");
  const partial = mo.days_read < mo.days_total;
  const wkMeet = accs.reduce((n, a) => n + wk[a.id].meetings, 0), moMeet = accs.reduce((n, a) => n + mo.accounts[a.id].meetings, 0);
  setHTML("hours", `<div class="hours">
      <span class="head"></span><span class="head">Esta semana</span><span class="head">Em ${monthName}</span>
      ${rows}
      <div class="who total">Total</div>
      <div class="bar num total"><span><span class="short-only">semana </span>${weekBusy ? `<b>${fmtDur(weekBusy)}</b> · ${wkMeet} reun.` : "—"}</span></div>
      <div class="month num total"><span class="short-only">${monthName} </span>${mo.busy_minutes ? `<b>${fmtDur(mo.busy_minutes)}</b> · ${moMeet} reun.` : "—"}</div>
    </div>
    <p class="hint">Reuniões no mesmo horário contam uma vez só no total.</p>
    ${partial ? `<div class="note"><span>${monthName[0].toUpperCase() + monthName.slice(1)}: ${mo.days_read} de ${mo.days_total} dias lidos.</span>
      <button data-readmonth>Ler o mês inteiro</button></div>` : ""}`);
}

function renderClashes(pairs) {
  const cross = pairs.filter((p) => p.cross), same = pairs.filter((p) => !p.cross);
  const shown = showSameClashes ? [...cross, ...same] : cross;
  const item = ({ date, a, b }) => {
    const wd = new Date(date + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "short", day: "numeric" }).replace(".", "");
    return `<li><button data-day="${date}" data-focus="${esc(evKey(a))}">
      <span class="when num">${wd} · ${a.start}</span>
      <span class="what">${badge(a.account, true)}${esc(a.title)} × ${badge(b.account, true)}${esc(b.title)}</span></button></li>`;
  };
  const head = cross.length ? "" : `<li class="none">Nenhum conflito entre empresas nesta semana.</li>`;
  const tail = same.length && !showSameClashes
    ? `<li class="same">+ ${plural(same.length, "sobreposição", "sobreposições")} dentro da mesma empresa. <button class="linklike" data-sameclash>Mostrar</button></li>` : "";
  setHTML("clashes", head + shown.map(item).join("") + tail);
}

function renderGrid(days) {
  const today = isoDate(new Date()), nowMin = nowMinutes();
  const weekday = (x) => { const n = new Date(x.d.date + "T12:00:00").getDay(); return n >= 1 && n <= 5; };
  const shown = days.filter((x) => weekday(x) || x.p.timed.length || x.p.allDay.length);
  $("week").style.setProperty("--cols", shown.length);
  setHTML("week", shown.map(({ d, p }) => {
    const date = new Date(d.date + "T12:00:00");
    const wd = date.toLocaleDateString("pt-BR", { weekday: "short" }).replace(".", "");
    const cross = p.active.some((e) => e.cross);
    const sub = (p.active.length ? `${plural(p.active.length, "reunião", "reuniões")} · ${fmtDur(p.busy)}${cross ? ` · <span class="warn">conflito</span>` : ""}`
      : p.allDay.length ? esc(p.allDay[0].title) : "livre");
    const isToday = d.date === today;
    const items = [...p.allDay.map((e) => wev(e, d.date, "dia", false)),
                   ...[...p.timed, ...p.acts].sort((x, y) => x.s - y.s)
                     .map((e) => wev(e, d.date, e.start, (isToday && e.e2 <= nowMin) || d.date < today))];
    return `<article class="wday${isToday ? " today" : ""}">
      <button data-day="${d.date}" title="Abrir o dia"><span class="dn num">${date.getDate()}</span><span class="wd">${wd}${isToday ? " · hoje" : ""}</span><span class="ws num">${sub}</span></button>
      ${items.length ? `<ol>${items.join("")}</ol>` : `<div class="free">Sem reuniões</div>`}
    </article>`;
  }).join("") + `<div class="wlegend">${days[0].d.accounts.map((a) => badge(a, true)).join("")}<span><span class="o"></span>talvez ou sem resposta</span><span><span class="sw"></span>conflito entre empresas</span></div>`);
}

function wev(e, date, time, past) {
  if (e.mine) {
    return `<li class="wev mine${past ? " past" : ""}" data-day="${date}" tabindex="0" role="button" style="--c:${ME.color}"
      aria-label="${esc(`${time}, ${e.title}, atividade sua. Abrir o dia`)}" title="Atividade sua">
      <span class="pin"></span><span class="t num">${time}<small>EU</small></span><span class="n">${esc(e.title)}</span></li>`;
  }
  const state = e.skipped ? "declined" : e.response;
  const cls = ["wev", state, e.cross && !past && "cross", past && "past"].filter(Boolean).join(" ");
  const status = { tentative: ", talvez", unknown: e.account.provider !== "google" ? ", sem resposta" : "", declined: ", recusada" }[e.response] || "";
  const desc = `${e.account.name}${e.skipped ? ", você não vai" : status}${e.cross ? ", em conflito com outra empresa" : ""}`;
  return `<li class="${cls}" data-day="${date}" data-focus="${esc(evKey(e))}" tabindex="0" role="button" style="--c:${esc(e.account.color)}"
      aria-label="${esc(`${time}, ${e.title}, ${desc}. Abrir o dia`)}" title="${esc(desc)}">
    <span class="pin"></span><span class="t num">${time}<small>${esc(e.account.short || "")}</small></span><span class="n">${esc(e.title)}</span></li>`;
}

// ---------- Lembretes ----------
// Funcionam enquanto esta aba estiver aberta (o navegador mostra a notificação mesmo minimizado)

const REMIND_BEFORE = 5;
let remindOn = localStorage.getItem("agenda.remind") === "on";
let todayData = null, todayFetched = 0;
const notified = new Set(JSON.parse(sessionStorage.getItem("agenda.notified") || "[]"));

function renderRemind() {
  const perm = "Notification" in window ? Notification.permission : "denied";
  const on = remindOn && perm === "granted";
  $("remind").setAttribute("aria-pressed", String(on));
  $("remind-label").textContent = perm === "denied" && remindOn ? "Lembretes bloqueados no navegador"
    : on ? `Lembretes ${REMIND_BEFORE} min antes` : "Lembretes desligados";
}

async function toggleRemind() {
  if (!("Notification" in window)) { remindOn = true; renderRemind(); return; }
  if (!remindOn && Notification.permission === "default") await Notification.requestPermission();
  remindOn = !remindOn;
  localStorage.setItem("agenda.remind", remindOn ? "on" : "off");
  renderRemind();
  if (remindOn) checkReminders();
}

async function checkReminders() {
  if (!remindOn || !("Notification" in window) || Notification.permission !== "granted") return;
  const today = isoDate(new Date());
  if (data && data.date === today) todayData = data;
  else if (!todayData || todayData.date !== today || Date.now() - todayFetched > 5 * 60_000) {
    try { todayData = await (await fetch(`/api/agenda?date=${today}`)).json(); todayFetched = Date.now(); } catch { return; }
  }
  const nowMin = nowMinutes();
  for (const e of prepare(todayData.accounts, todayData.plan).active) {
    const left = e.s - nowMin, key = `${today}|${evKey(e)}`;
    if (left > 0 && left <= REMIND_BEFORE && !notified.has(key)) {
      notified.add(key);
      sessionStorage.setItem("agenda.notified", JSON.stringify([...notified]));
      new Notification(e.title, { body: `${e.start}–${e.end || ""} · ${e.account.name} · em ${left} min`, tag: key, lang: "pt-BR" });
    }
  }
}

// ---------- Interação ----------

// Login: a janela abre já no clique (abrir depois de um await cai no bloqueador de pop-up)
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button, [data-goto], [data-day]");
  if (!btn) return;
  if (btn.dataset.choose) {
    const others = JSON.parse(btn.dataset.others);
    api("POST", "/api/plan/skip", { date: data.date, keys: others, skip: true })
      .then(() => api("POST", "/api/plan/skip", { date: data.date, keys: [btn.dataset.choose], skip: false }))
      .then(afterPlanChange).catch((e) => { $("alert").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
    return;
  }
  if (btn.dataset.unskip) {
    api("POST", "/api/plan/skip", { date: data.date, keys: [btn.dataset.unskip], skip: false }).then(afterPlanChange);
    return;
  }
  if (btn.dataset.addact) { formGap = btn.dataset.addact; render(); return; }
  if ("cancelact" in btn.dataset) { formGap = null; render(); return; }
  if (btn.dataset.delact) { api("DELETE", `/api/plan/activities/${btn.dataset.delact}`).then(afterPlanChange); return; }
  if (btn.dataset.day) {
    pendingFocus = btn.dataset.focus || null;
    setView("day");
    go(btn.dataset.day);
  } else if ("sameclash" in btn.dataset) {
    showSameClashes = true;
    render();
  } else if ("readmonth" in btn.dataset) {
    btn.disabled = true; btn.textContent = "Lendo o mês…";
    load({ method: "POST", span: "month" });
  } else if (btn.dataset.goto) {
    const el = $(btn.dataset.goto);
    if (!el) return;
    el.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  } else if (btn.dataset.login) {
    const blank = !vncUrl;
    const win = window.open(vncUrl || "about:blank", "agenda-vnc");
    fetch(`/api/accounts/${encodeURIComponent(btn.dataset.login)}/login`, { method: "POST" })
      .then(() => { if (win && blank && vncUrl) win.location.href = vncUrl; });
    btn.textContent = "Já entrei, atualizar";
    delete btn.dataset.login;
    btn.dataset.retry = "";
  } else if ("retry" in btn.dataset) {
    load({ method: "POST" });
  }
});

function go(iso) { current = iso; formGap = null; load(); }
function step() { return view === "week" ? 7 : 1; }
function setView(v) {
  view = v;
  showSameClashes = false;
  localStorage.setItem("agenda.view", v);
  render();
}
$("prev").onclick = () => go(shiftDay(current, -step()));
$("next").onclick = () => go(shiftDay(current, step()));
$("today").onclick = () => go(isoDate(new Date()));
$("refresh").onclick = () => load({ method: "POST" });
VIEWS.forEach((v) => { $(`view-${v}`).onclick = () => { setView(v); load(); }; });
$("theme").onclick = () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  localStorage.setItem("mana.theme", dark ? "dark" : "light");
  syncTheme();
};
function syncTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("theme").setAttribute("aria-pressed", String(dark));
  $("theme").setAttribute("aria-label", dark ? "Tema claro" : "Tema preto");
  $("theme").title = dark ? "Tema claro" : "Tema preto";
}
syncTheme();
$("remind").onclick = toggleRemind;

document.addEventListener("keydown", (ev) => {
  // Itens da semana são <li> clicáveis: Enter e espaço abrem o dia, como num botão
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches?.("li[data-day]")) { ev.preventDefault(); ev.target.click(); return; }
  if (ev.key === "Escape" && formGap) { formGap = null; render(); return; }
  if (ev.key === "Escape" && tagEditing) { tagEditing = null; renderTodo(); return; }
  if (ev.metaKey || ev.ctrlKey || ev.altKey || /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
  const k = ev.key.toLowerCase();
  const cal = view === "day" || view === "week";
  if (k === "arrowleft" && cal) go(shiftDay(current, -step()));
  else if (k === "arrowright" && cal) go(shiftDay(current, step()));
  else if (k === "d" && view !== "day") { setView("day"); load(); }
  else if (k === "s" && view !== "week") { setView("week"); load(); }
  else if (k === "f" && view !== "todo") { setView("todo"); load(); }
  else if (k === "c" && view !== "accounts") { setView("accounts"); load(); }
  else if (k === "t" && cal) go(isoDate(new Date()));
  else if (k === "r" && cal) load({ method: "POST" });
  else return;
  ev.preventDefault();
});

// ---------- Título da aba (o pomodoro rodando põe o tempo na frente) ----------

let baseTitle = document.title;
function setTitle(t) { baseTitle = t; applyTitle(); }
function applyTitle() {
  document.title = pst && pst.running ? `${fmtClock(pomoLeft())} · ${POMO_LABEL[pst.mode]} · ManaTime` : baseTitle;
}
function todayIso() { return isoDate(new Date()); }
function headerFor(title, date, summary) {
  $("title").textContent = title;
  $("date").textContent = date;
  setHTML("summary", summary);
  setTitle(`${title} · ManaTime`);
}
function longDate(iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

// ---------- Pomodoro ----------
// Roda no navegador: o fim da etapa é guardado como horário, então continua certo com a aba em segundo plano ou recarregada

const POMO_DEFAULT = { focus: 25, short: 5, long: 15, cycles: 4, auto: false, sound: true };
const POMO_LABEL = { focus: "Foco", short: "Pausa curta", long: "Pausa longa" };
let pcfg = { ...POMO_DEFAULT, ...JSON.parse(localStorage.getItem("mana.pomo.cfg") || "{}") };
const pdur = (mode) => ({ focus: pcfg.focus, short: pcfg.short, long: pcfg.long }[mode]) * 60000;
let pst = JSON.parse(localStorage.getItem("mana.pomo.state") || "null")
  || { mode: "focus", running: false, endsAt: 0, left: pdur("focus"), done: 0, taskId: null, taskTitle: null };

function pomoSave() { localStorage.setItem("mana.pomo.state", JSON.stringify(pst)); }
function pomoLeft() { return pst.running ? Math.max(0, pst.endsAt - Date.now()) : pst.left; }
function fmtClock(ms) {
  const sec = Math.ceil(ms / 1000);
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}
function pomoStats() {
  const st = JSON.parse(localStorage.getItem("mana.pomo.stats") || "{}");
  return { day: st.day === todayIso() ? st.count || 0 : 0, perTask: st.perTask || {} };
}
function recordFocus() {
  const st = pomoStats();
  const perTask = { ...st.perTask };
  if (pst.taskId) perTask[pst.taskId] = (perTask[pst.taskId] || 0) + 1;
  localStorage.setItem("mana.pomo.stats", JSON.stringify({ day: todayIso(), count: st.day + 1, perTask }));
}

function pomoToggle() {
  pomoNote = "";
  if (pst.running) { pst.left = pomoLeft(); pst.running = false; }
  else { pst.running = true; pst.endsAt = Date.now() + pst.left; }
  pomoSave(); pomoRender(); applyTitle();
}
function pomoReset() { pst.running = false; pst.left = pdur(pst.mode); pomoSave(); pomoRender(); applyTitle(); }
// Avança para a próxima etapa. Só conta o foco que chegou ao fim (pular não conta).
function pomoNext(finished) {
  if (pst.mode === "focus") {
    if (finished) { recordFocus(); pst.done += 1; }
    pst.mode = finished && pst.done % pcfg.cycles === 0 ? "long" : "short";
  } else {
    pst.mode = "focus";
  }
  pst.left = pdur(pst.mode);
  pst.running = Boolean(finished && pcfg.auto);
  if (pst.running) pst.endsAt = Date.now() + pst.left;
  pomoSave(); pomoRender(); applyTitle();
}

function beep() {
  try {
    const ctx = new AudioContext();
    [0, 0.35].forEach((t) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.3);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.32);
    });
  } catch {}
}

let pomoNote = "";
// late = a etapa terminou enquanto a aba estava fechada: sem som atrasado, só um aviso
function pomoFinish(late = false) {
  const was = pst.mode;
  if (late) {
    const at = new Date(pst.endsAt).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    pomoNote = `${POMO_LABEL[was]} terminou às ${at}, com a aba fechada.`;
    pomoNext(true);
    pst.running = false; pst.left = pdur(pst.mode); pomoSave();   // não começa sozinho atrasado
    if (view === "todo") renderTodo();
    return;
  }
  pomoNote = "";
  if (pcfg.sound) beep();
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(was === "focus" ? "Foco concluído" : "Pausa terminou",
      { body: was === "focus" ? "Hora de uma pausa." : "Hora de voltar ao foco.", tag: "mana-pomo", lang: "pt-BR" });
  }
  pomoNext(true);
  if (view === "todo") renderTodo();   // contagem da tarefa e do dia
}

function pomoRender() {
  if (view !== "todo") return;
  const left = pomoLeft(), total = pdur(pst.mode);
  $("pomo-time").textContent = fmtClock(left);
  $("pomo-mode").textContent = POMO_LABEL[pst.mode];
  document.querySelector(".pomo").classList.toggle("brk", pst.mode !== "focus");
  $("pomo-prog").style.strokeDashoffset = String(603.19 * (1 - left / total));
  const inSet = pst.done % pcfg.cycles;
  $("pomo-dots").innerHTML = Array.from({ length: pcfg.cycles }, (_, i) => `<i class="${i < inSet ? "on" : ""}"></i>`).join("");
  $("pomo-start").innerHTML = pst.running ? `<svg class="i"><use href="#i-pause"/></svg><span>Pausar</span>`
    : `<svg class="i"><use href="#i-play"/></svg><span>${left < total ? "Continuar" : "Iniciar"}</span>`;
  $("pomo-task").innerHTML = pst.taskTitle
    ? `Focando em <b>${esc(pst.taskTitle)}</b> · <button class="linklike" data-unfocus>soltar</button>`
    : "Escolha uma tarefa com “Focar” (opcional).";
  if (pomoNote) $("pomo-task").innerHTML = `<span class="late">${esc(pomoNote)}</span>`;
  const n = pomoStats().day;
  $("pomo-stats").textContent = `${n} ${n === 1 ? "pomodoro concluído" : "pomodoros concluídos"} hoje`;
}

setInterval(() => {
  if (pst.running && pomoLeft() <= 0) pomoFinish(Date.now() - pst.endsAt > 10_000);
  if (pst.running) { pomoRender(); applyTitle(); }
}, 500);

function fillPomoForm() {
  const f = $("pomo-form");
  ["focus", "short", "long", "cycles"].forEach((k) => { f[k].value = pcfg[k]; });
  f.auto.checked = pcfg.auto; f.sound.checked = pcfg.sound;
}
$("pomo-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target, n = (k) => Number(f[k].value);
  const bad = ["focus", "short", "long", "cycles"].find((k) => !Number.isInteger(n(k)) || n(k) < Number(f[k].min) || n(k) > Number(f[k].max));
  if (bad) { $("pomo-err").textContent = "Use números inteiros dentro dos limites de cada campo."; return; }
  pcfg = { focus: n("focus"), short: n("short"), long: n("long"), cycles: n("cycles"), auto: f.auto.checked, sound: f.sound.checked };
  localStorage.setItem("mana.pomo.cfg", JSON.stringify(pcfg));
  $("pomo-err").textContent = "";
  if (!pst.running) pst.left = pdur(pst.mode);   // etapa parada assume a nova duração
  pomoSave(); pomoRender();
  f.closest("details").open = false;
});
$("pomo-defaults").onclick = () => { pcfg = { ...POMO_DEFAULT }; fillPomoForm(); };
$("pomo-start").onclick = pomoToggle;
$("pomo-reset").onclick = pomoReset;
$("pomo-skip").onclick = () => pomoNext(false);

// ---------- To-do ----------

let todoTag = null;        // filtro ativo
let tagEditing = null;     // tarefa com as tags em edição
// "#tag" no texto vira tag; letras com acento, números, - e _
const TAG_RE = /(^|\s)#([\p{L}\p{N}_-]{1,24})/gu;
function splitTags(text) {
  const tags = [];
  const title = text.replace(TAG_RE, (_, sp, t) => { tags.push(t.toLowerCase()); return sp; }).replace(/\s+/g, " ").trim();
  return { title, tags: [...new Set(tags)] };
}

async function loadTodo() {
  try {
    tdata = await (await fetch("/api/todo")).json();
    $("alert").innerHTML = "";
  } catch { $("alert").innerHTML = `<div class="alert">O serviço não respondeu. Confira se o container está rodando.</div>`; }
  render();
}

function dueLabel(t) {
  if (!t.date) return t.time ? t.time : "";
  const today = todayIso();
  const d = t.date === today ? "hoje" : t.date === shiftDay(today, 1) ? "amanhã" : t.date === shiftDay(today, -1) ? "ontem"
    : new Date(t.date + "T12:00:00").toLocaleDateString("pt-BR", { day: "numeric", month: "short" }).replace(".", "");
  const late = t.date < today && !t.done;
  return `<span class="${late ? "late" : ""}">${d}${t.time ? ` · ${t.time}` : ""}</span>`;
}

function todoItem(t) {
  const pom = pomoStats().perTask[t.id] || 0;
  const meta = [dueLabel(t), pom ? plural(pom, "pomodoro", "pomodoros") : ""].filter(Boolean).join(" · ");
  const focused = pst.taskId === t.id;
  const tags = t.tags || [];
  const chips = tags.length ? `<span class="tags-row">${tags.map((g) =>
    `<button class="chip" data-tagfilter="${esc(g)}" aria-pressed="${todoTag === g}" title="Filtrar por #${esc(g)}">#${esc(g)}</button>`).join("")}</span>` : "";
  const editor = tagEditing === t.id ? `<form class="tagedit" data-tagform="${esc(t.id)}">
      <input name="tags" value="${esc([t.title, ...tags.map((g) => "#" + g)].join(" "))}" placeholder="Título #tag" aria-label="Título e tags de ${esc(t.title)}">
      <button type="submit" class="primary">Salvar</button><button type="button" data-tagcancel>Cancelar</button></form>` : "";
  return `<li class="titem${t.done ? " done" : ""}${focused ? " focused" : ""}">
    <input type="checkbox" id="td-${esc(t.id)}" data-todo="${esc(t.id)}"${t.done ? " checked" : ""}>
    <div><label for="td-${esc(t.id)}">${esc(t.title)}${meta ? `<span class="tmeta">${meta}</span>` : ""}</label>${chips}${editor}</div>
    <span class="tact">
      <button class="ghost" data-edittags="${esc(t.id)}" aria-label="Editar ${esc(t.title)}" title="Editar título e tags">Editar</button>
      ${t.done ? "" : `<button class="ghost" data-focustask="${esc(t.id)}" data-title="${esc(t.title)}">${focused ? "Focando" : "Focar"}</button>`}
      <button class="ghost" data-deltodo="${esc(t.id)}" aria-label="Remover tarefa ${esc(t.title)}" title="Remover"><svg class="i"><use href="#i-x"/></svg></button>
    </span></li>`;
}

function renderTodo() {
  let t = tdata || { overdue: [], today: [], later: [], nodate: [], done: [] };
  const open = t.overdue.length + t.today.length + t.later.length + t.nodate.length;
  headerFor("To-do", longDate(todayIso()),
    [`<b>${plural(open, "tarefa aberta", "tarefas abertas")}</b>`, t.overdue.length ? `<span class="warn">${t.overdue.length} atrasada${t.overdue.length > 1 ? "s" : ""}</span>` : null,
     `${plural(pomoStats().day, "pomodoro", "pomodoros")} hoje`].filter(Boolean).join(" · "));
  $("todo-count").textContent = open ? String(open) : "";
  // barra de filtros: todas as tags das tarefas abertas, com quantidade
  const openAll = [...t.overdue, ...t.today, ...t.later, ...t.nodate];
  const counts = {};
  openAll.forEach((x) => (x.tags || []).forEach((g) => { counts[g] = (counts[g] || 0) + 1; }));
  if (todoTag && !counts[todoTag]) todoTag = null;
  const names = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  $("tagbar").innerHTML = names.length ? `<button class="chip" data-tagfilter="" aria-pressed="${!todoTag}">Todas</button>` +
    names.map((g) => `<button class="chip" data-tagfilter="${esc(g)}" aria-pressed="${todoTag === g}">#${esc(g)}<span class="n">${counts[g]}</span></button>`).join("") : "";
  const keep = (list) => todoTag ? list.filter((x) => (x.tags || []).includes(todoTag)) : list;
  t = { ...t, overdue: keep(t.overdue), today: keep(t.today), later: keep(t.later), nodate: keep(t.nodate) };
  const group = (cls, title, items) => items.length ? `<div class="tgroup ${cls}"><h3>${title}</h3><ul class="tlist">${items.map(todoItem).join("")}</ul></div>` : "";
  const shown = t.overdue.length + t.today.length + t.later.length + t.nodate.length;
  $("todo-lists").innerHTML = (open && !shown ? `<p class="emptytodo">Nenhuma tarefa aberta com #${esc(todoTag)}.</p>` : "") + (open ? group("late", "Atrasadas", t.overdue) + group("", "Hoje", t.today) + group("", "Próximas", t.later) + group("", "Sem data", t.nodate)
    : `<p class="emptytodo">Nada pendente. Adicione uma tarefa acima.</p>`)
    + (t.done.length ? `<details class="tgroup donegroup"><summary>Feitas · ${t.done.length}</summary><ul class="tlist">${t.done.map(todoItem).join("")}</ul></details>` : "");
  fillPomoForm();
  pomoRender();
}

$("todo-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const { title, tags } = splitTags($("todo-title").value);
  if (!title) { $("todo-err").textContent = tags.length ? "Escreva a tarefa além das tags." : ""; $("todo-title").focus(); return; }
  const time = parseHHMM($("todo-time").value);
  if (time === null) { $("todo-err").textContent = "Horário inválido. Use o formato 14:30."; $("todo-time").focus(); return; }
  const when = $("todo-when").value;
  const date = when === "today" ? todayIso() : when === "tomorrow" ? shiftDay(todayIso(), 1) : null;
  try {
    await api("POST", "/api/plan/tasks", { date, title, time: time || null, tags: tags.length ? tags : todoTag ? [todoTag] : [] });
    $("todo-title").value = ""; $("todo-time").value = ""; $("todo-err").textContent = "";
    await loadTodo();
    $("todo-title").focus();
  } catch (e) { $("todo-err").textContent = e.message; }
});

document.addEventListener("change", async (ev) => {
  const id = ev.target.dataset?.todo;
  if (!id) return;
  try {
    await api("PATCH", `/api/plan/tasks/${id}`, { done: ev.target.checked });
    if (ev.target.checked && pst.taskId === id) { pst.taskId = null; pst.taskTitle = null; pomoSave(); }
    if (view === "todo") await loadTodo(); else await load({ quiet: true });
  } catch (e) { ev.target.checked = !ev.target.checked; (view === "todo" ? $("todo-err") : $("alert")).textContent = e.message; }
});

document.addEventListener("submit", async (ev) => {
  const id = ev.target.dataset?.tagform;
  if (!id) return;
  ev.preventDefault();
  const { title, tags } = splitTags(ev.target.tags.value);
  if (!title) { $("todo-err").textContent = "O título não pode ficar vazio."; return; }
  try { await api("PATCH", `/api/plan/tasks/${id}`, { title, tags }); tagEditing = null; await loadTodo(); }
  catch (e) { $("todo-err").textContent = e.message; }
});

// ---------- Contas ----------

const PROVIDER_LABEL = { google: "Google Workspace / Gmail", outlook: "Microsoft 365", outlook_live: "Outlook pessoal", custom: "Outro (endereço do calendário)" };
let accEditing = null, accRemoving = null, accNotice = "";

async function loadAccounts() {
  try {
    adata = await (await fetch("/api/accounts")).json();
    if (!data) data = await (await fetch(`/api/agenda?date=${todayIso()}`)).json();   // para mostrar o status de cada conta
    $("alert").innerHTML = "";
  } catch { $("alert").innerHTML = `<div class="alert">O serviço não respondeu. Confira se o container está rodando.</div>`; }
  render();
}

function swatches(name, current) {
  const colors = adata.colors;
  const custom = !colors.includes(current);
  return `<div class="swatches" role="radiogroup" aria-label="Cor">${colors.map((c) => `<label title="${c}">
      <input type="radio" name="${name}" value="${c}"${c === current ? " checked" : ""}><span class="sw" style="--c:${c}"></span></label>`).join("")}
    <label title="Outra cor"><input type="color" name="${name}-custom" value="${custom ? current : "#888888"}" aria-label="Outra cor"></label></div>`;
}

function accFields(a, isNew) {
  const used = new Set(adata.accounts.map((x) => x.color));
  const color = a.color || adata.colors.find((c) => !used.has(c)) || adata.colors[0];
  return `
    <label class="f">Nome<input name="name" maxlength="60" required value="${esc(a.name || "")}" placeholder="Ex.: Empresa X"></label>
    <label class="f">Sigla (até 4 letras)<input name="short" maxlength="4" required value="${esc(a.short || "")}" placeholder="EMX"></label>
    ${isNew ? `<label class="f">Tipo de conta<select name="provider">${adata.providers.map((p) => `<option value="${p}"${p === (a.provider || "outlook") ? " selected" : ""}>${PROVIDER_LABEL[p]}</option>`).join("")}</select></label>` : ""}
    <div class="f${isNew ? "" : " wide"}">Cor${swatches("color", color)}</div>
    <label class="f wide url-field"${(a.provider || "outlook") === "custom" ? "" : " hidden"}>Endereço do calendário (use {y}, {m} e {d} no lugar da data)
      <input name="url" maxlength="500" value="${esc(a.url || "")}" placeholder="https://calendario.exemplo.com/dia/{y}-{m}-{d}"></label>
    <p class="formerr" role="alert"></p>`;
}

function readAccForm(f) {
  const custom = f.querySelector('input[type=radio]:checked') ? null : f.querySelector('input[type=color]').value;
  return {
    name: f.name.value.trim(), short: f.short.value.trim().toUpperCase(),
    color: custom || f.querySelector('input[type=radio]:checked').value,
    ...(f.provider ? { provider: f.provider.value } : {}),
    ...(f.url && !f.querySelector(".url-field").hidden ? { url: f.url.value.trim() } : {}),
  };
}

function renderAccountsView() {
  if (!adata) return;
  const status = Object.fromEntries((data?.accounts || []).map((a) => [a.id, a]));
  const n = adata.accounts.length;
  const pending = adata.accounts.filter((a) => status[a.id]?.status === "login_required").length;
  headerFor("Contas", plural(n, "conta conectada", "contas conectadas"),
    pending ? `<span class="warn">${plural(pending, "conta precisa", "contas precisam")} de login</span>` : "Edite, reordene, adicione ou remova as agendas lidas pela ManaTime.");
  $("acc-list").innerHTML = adata.accounts.map((a, i) => {
    const st = status[a.id];
    const s = st ? accountStatus(st) : { text: "ainda não lida" };
    const stText = st?.status === "ok" && !st.missing ? `<span class="ok">sincronizada</span>` : `<span class="${s.bad ? "bad" : ""}">${esc(s.text.replace(/<[^>]+>/g, ""))}</span>`;
    let extra = "";
    if (accEditing === a.id) {
      extra = `<form class="acc-edit acc-form" data-editacc="${esc(a.id)}">${accFields(a, false)}
        <div class="row-actions"><button type="submit" class="primary">Salvar</button><button type="button" data-cancelacc>Cancelar</button></div></form>`;
    } else if (accRemoving === a.id) {
      extra = `<div class="acc-confirm"><p>Remover <b>${esc(a.name)}</b> da ManaTime? As reuniões dela somem do dashboard.</p>
        <label><input type="checkbox" id="forget-login"> Apagar também o login salvo (sem isso, dá para recolocar a conta sem logar de novo)</label>
        <div class="row-actions"><button class="danger" data-confirmremove="${esc(a.id)}">Remover conta</button><button data-cancelacc>Cancelar</button></div></div>`;
    }
    return `<li class="acc-row" style="--c:${esc(a.color)}">
      <span class="dot" aria-hidden="true"></span>
      <div class="who"><b>${esc(a.name)}</b> ${badge(a, true)}<small>${PROVIDER_LABEL[a.provider]} · ${stText}</small></div>
      <div class="ops">
        ${st?.status === "login_required" || !a.has_login ? `<button data-login="${esc(a.id)}">Fazer login</button>` : ""}
        <button data-editacc-open="${esc(a.id)}">Editar</button>
        <button class="icon" data-moveacc="${esc(a.id)}" data-step="-1" aria-label="Subir ${esc(a.name)}" title="Subir"${i === 0 ? " disabled" : ""}><svg class="i"><use href="#i-up"/></svg></button>
        <button class="icon" data-moveacc="${esc(a.id)}" data-step="1" aria-label="Descer ${esc(a.name)}" title="Descer"${i === n - 1 ? " disabled" : ""}><svg class="i"><use href="#i-down"/></svg></button>
        <button class="ghost" data-removeacc="${esc(a.id)}">Remover</button>
      </div>${extra}</li>`;
  }).join("") || `<li class="emptytodo">Nenhuma conta. Adicione a primeira abaixo.</li>`;
  const add = $("acc-add");
  if (!add.dataset.ready || add.dataset.count !== String(n)) {
    add.innerHTML = accFields({}, true) + `<div class="row-actions"><button type="submit" class="primary">Adicionar conta</button></div>
      ${accNotice ? `<p class="okmsg">${accNotice}</p>` : ""}`;
    add.dataset.ready = "1"; add.dataset.count = String(n);
  }
}

async function accountsChanged(msg) {
  accEditing = accRemoving = null;
  adata = await (await fetch("/api/accounts")).json();
  data = await (await fetch(`/api/agenda?date=${todayIso()}`)).json();
  wdata = null;
  accNotice = msg || "";
  $("acc-add").dataset.ready = "";
  render();
}

document.addEventListener("input", (ev) => {
  const f = ev.target.form;
  if (!f || !f.classList?.contains("acc-form")) return;
  if (ev.target.name === "provider") f.querySelector(".url-field").hidden = ev.target.value !== "custom";
  if (ev.target.type === "color") f.querySelectorAll('input[type=radio]').forEach((r) => { r.checked = false; });
  // sigla sugerida a partir do nome, até a pessoa digitar uma
  if (ev.target.name === "name" && f.id === "acc-add" && !f.short.dataset.touched) {
    f.short.value = ev.target.value.normalize("NFD").replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
  }
  if (ev.target.name === "short") f.short.dataset.touched = "1";
});

document.addEventListener("submit", async (ev) => {
  const f = ev.target;
  if (f.id !== "acc-add" && !f.dataset.editacc) return;
  ev.preventDefault();
  const body = readAccForm(f), err = (m) => { f.querySelector(".formerr").textContent = m; };
  if (!body.name || !body.short) return err("Preencha nome e sigla.");
  try {
    if (f.id === "acc-add") {
      const a = await api("POST", "/api/accounts", body);
      const msg = `<b>${esc(a.name)}</b> adicionada. Use “Fazer login” na lista acima para entrar nela.`;
      await accountsChanged(msg);
      // primeira leitura em segundo plano: mostra se a conta já está logada ou precisa de login
      fetch(`/api/refresh?date=${todayIso()}&span=week`, { method: "POST" })
        .then(() => { if (view === "accounts") accountsChanged(msg); });
    } else {
      await api("PATCH", `/api/accounts/${f.dataset.editacc}`, body);
      await accountsChanged();
    }
  } catch (e) { err(e.message); }
});

// Cliques das seções novas (o tratador geral de cliques continua cuidando de login, dia e conflitos)
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  if ("tagfilter" in d) { todoTag = d.tagfilter || null; renderTodo(); return; }
  if (d.edittags) { tagEditing = tagEditing === d.edittags ? null : d.edittags; renderTodo();
    document.querySelector(`[data-tagform="${CSS.escape(d.edittags)}"] input`)?.focus(); return; }
  if ("tagcancel" in d) { tagEditing = null; renderTodo(); return; }
  if (d.focustask) {
    pst.taskId = d.focustask; pst.taskTitle = d.title; pomoSave(); renderTodo();
    document.querySelector(".pomo").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else if ("unfocus" in d) { pst.taskId = null; pst.taskTitle = null; pomoSave(); renderTodo(); }
  else if (d.deltodo) { await api("DELETE", `/api/plan/tasks/${d.deltodo}`); await loadTodo(); }
  else if (d.editaccOpen) { accEditing = d.editaccOpen; accRemoving = null; render(); }
  else if (d.removeacc) { accRemoving = d.removeacc; accEditing = null; render(); }
  else if ("cancelacc" in d) { accEditing = accRemoving = null; render(); }
  else if (d.moveacc) { await api("POST", `/api/accounts/${d.moveacc}/move?step=${d.step}`); await accountsChanged(); }
  else if (d.confirmremove) {
    const forget = $("forget-login")?.checked ? "?forget_login=true" : "";
    await api("DELETE", `/api/accounts/${d.confirmremove}${forget}`);
    await accountsChanged("Conta removida.");
  }
});

render();
renderRemind();
load();
setInterval(() => { if (!formGap && !document.activeElement?.matches("input")) render(); checkReminders(); }, 30_000);    // move o "agora" e confere lembretes
setInterval(() => { if (!formGap) load({ quiet: true }); }, 5 * 60_000);         // pega o cache da atualização automática
