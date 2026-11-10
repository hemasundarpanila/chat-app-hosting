const me = document.body.dataset.me;
const socket = io();
let current = null;
const $ = id => document.getElementById(id);
const fmt = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});

function addMsg(m) {
  const d = document.createElement("div");
  d.className = "msg " + (m.sender === me ? "mine" : "theirs");
  const t = document.createElement("span"); t.textContent = m.body;
  const s = document.createElement("small"); s.textContent = fmt(m.ts);
  d.append(t, s);
  $("msgs").append(d);
  $("msgs").scrollTop = $("msgs").scrollHeight;
}

async function loadChats() {
  const list = await (await fetch("/api/chats")).json();
  const ul = $("chats"); ul.innerHTML = "";
  list.forEach(p => {
    const li = document.createElement("li"), b = document.createElement("button");
    b.textContent = p; if (p === current) b.className = "on";
    b.onclick = () => openChat(p);
    li.append(b); ul.append(li);
  });
}

async function openChat(p) {
  current = p; $("title").textContent = p;
  document.body.classList.add("inchat");
  $("msgs").innerHTML = "";
  (await (await fetch("/api/messages/" + p)).json()).forEach(addMsg);
  loadChats(); $("txt").focus();
}

$("addForm").onsubmit = async e => {
  e.preventDefault();
  const r = await fetch("/api/user/" + encodeURIComponent($("num").value)), j = await r.json();
  if (!r.ok) { $("addErr").textContent = j.error; return; }
  $("addErr").textContent = ""; $("num").value = ""; openChat(j.phone);
};
$("sendForm").onsubmit = e => {
  e.preventDefault();
  const body = $("txt").value.trim();
  if (!body || !current) return;
  socket.emit("send", {to: current, body}); $("txt").value = "";
};
socket.on("message", m => {
  if ((m.sender === me ? m.receiver : m.sender) === current) addMsg(m);
  loadChats();
});
$("back").onclick = () => document.body.classList.remove("inchat");
loadChats();
