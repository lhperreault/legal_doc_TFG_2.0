/**
 * Law Firm Client Chatbot Widget
 *
 * Embeddable chat widget adapted from LP Pressure Washing chatbot.
 * Features: pre-chat email form, configurable branding, server-driven suggestions.
 *
 * Usage:
 *   <script src="https://YOUR_SERVER/widget.js"
 *     data-firm-name="Your Law Firm"
 *     data-primary-color="#1e3a5f"
 *     data-practice-area="immigration"
 *     data-subtitle="Online - usually replies instantly">
 *   </script>
 */

(function () {
  // ─── Configuration from data attributes ──────────────────────────────────
  const scriptTag = document.currentScript;
  const ORIGIN = scriptTag?.src ? new URL(scriptTag.src).origin : "";
  const API_URL = ORIGIN + "/api/chat";
  const LOOKUP_URL = ORIGIN + "/api/clients/lookup";
  const STORAGE_KEY = "law-chat-session";

  const FIRM_NAME = scriptTag?.getAttribute("data-firm-name") || "Legal Assistant";
  const PRIMARY_COLOR = scriptTag?.getAttribute("data-primary-color") || "#1e3a5f";
  const PRACTICE_AREA = scriptTag?.getAttribute("data-practice-area") || "immigration";
  const SUBTITLE = scriptTag?.getAttribute("data-subtitle") || "Online - usually replies instantly";
  const LOGO_URL = scriptTag?.getAttribute("data-logo-url") || "";

  // ─── State ────────────────────────────────────────────────────────────────
  const messages = [];
  let isOpen = false;
  let isWaiting = false;
  let clientInfo = null;
  let conversationId = null;
  let identified = false; // Has the user submitted the pre-chat form?

  // ─── LocalStorage helpers ─────────────────────────────────────────────────
  function saveSession() {
    const data = { messages, clientInfo, conversationId, identified, ts: Date.now() };
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch {}
  }

  function loadSession() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      // Expire after midnight
      const saved = new Date(data.ts);
      if (saved.toDateString() !== new Date().toDateString()) {
        localStorage.removeItem(STORAGE_KEY);
        return null;
      }
      return data;
    } catch { return null; }
  }

  function clearSession() {
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
  }

  // ─── Styles ───────────────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    #law-chat-bubble {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 64px; height: 64px; border-radius: 50%;
      background: ${PRIMARY_COLOR}; border: none; padding: 0;
      box-shadow: 0 4px 16px rgba(0,0,0,0.25);
      cursor: pointer; overflow: hidden;
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.2s;
      font-size: 28px; color: #fff;
    }
    #law-chat-bubble:hover { transform: scale(1.08); }
    #law-chat-bubble img {
      width: 100%; height: 100%; object-fit: cover; border-radius: 50%;
    }
    #law-chat-window {
      position: fixed; bottom: 100px; right: 24px; z-index: 9999;
      width: 420px; max-width: calc(100vw - 32px);
      height: 560px; max-height: calc(100vh - 120px);
      background: #fff; border-radius: 16px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.18);
      display: flex; flex-direction: column; overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      transition: opacity 0.2s, transform 0.2s;
    }
    #law-chat-window.law-hidden {
      opacity: 0; transform: translateY(12px); pointer-events: none;
    }
    #law-chat-header {
      background: ${PRIMARY_COLOR}; color: #fff;
      padding: 10px 18px; display: flex; align-items: center; gap: 10px;
      flex-shrink: 0;
    }
    .law-header-icon {
      width: 36px; height: 36px; border-radius: 50%;
      background: rgba(255,255,255,0.15);
      display: flex; align-items: center; justify-content: center;
      font-size: 18px; flex-shrink: 0;
    }
    #law-chat-header-text h4 { margin: 0; font-size: 15px; font-weight: 600; }
    #law-chat-header-text p  { margin: 0; font-size: 12px; opacity: 0.85; }
    #law-chat-close {
      margin-left: auto; background: none; border: none;
      color: #fff; font-size: 20px; cursor: pointer; padding: 0 4px;
      opacity: 0.8; line-height: 1;
    }
    #law-chat-close:hover { opacity: 1; }

    /* Pre-chat form */
    #law-prechat {
      padding: 24px 20px; background: #f8fafc;
      border-bottom: 1px solid #e2e8f0;
      display: flex; flex-direction: column; gap: 12px;
    }
    #law-prechat h3 { margin: 0; font-size: 17px; font-weight: 700; color: #1e293b; }
    #law-prechat p { margin: 0; font-size: 13px; color: #64748b; line-height: 1.4; }
    #law-prechat input {
      padding: 10px 12px; border: 1px solid #e2e8f0; border-radius: 8px;
      font-size: 14px; font-family: inherit; outline: none;
    }
    #law-prechat input:focus { border-color: ${PRIMARY_COLOR}; }
    #law-prechat-submit {
      background: ${PRIMARY_COLOR}; color: #fff; border: none;
      border-radius: 8px; padding: 10px; font-size: 14px;
      cursor: pointer; font-family: inherit; font-weight: 600;
      transition: opacity 0.15s;
    }
    #law-prechat-submit:hover { opacity: 0.9; }
    #law-prechat-submit:disabled { opacity: 0.5; cursor: not-allowed; }
    #law-prechat.law-form-hidden { display: none; }

    /* Messages */
    #law-chat-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 10px;
      background: #f8fafc;
    }
    #law-chat-messages::-webkit-scrollbar { width: 4px; }
    #law-chat-messages::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 2px; }

    .law-msg-row {
      display: flex; align-items: flex-end; gap: 8px;
      align-self: flex-start; max-width: 88%;
    }
    .law-msg-avatar {
      width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0;
      background: ${PRIMARY_COLOR}; color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 700;
    }
    .law-msg {
      padding: 10px 14px; border-radius: 14px; font-size: 14px;
      line-height: 1.5; word-break: break-word; white-space: pre-wrap;
    }
    .law-msg-bot {
      background: #fff; color: #1e293b;
      border-bottom-left-radius: 4px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    }
    .law-msg-user {
      background: ${PRIMARY_COLOR}; color: #fff;
      border-bottom-right-radius: 4px;
      align-self: flex-end; max-width: 82%;
    }

    /* Suggestions */
    .law-suggestions {
      display: flex; flex-wrap: wrap; gap: 6px;
      align-self: flex-start; padding-left: 36px;
    }
    .law-suggestion-btn {
      background: #fff; color: #64748b; border: 1.5px solid #cbd5e1;
      border-radius: 20px; padding: 6px 14px; font-size: 13px;
      cursor: pointer; font-family: inherit;
      transition: background 0.15s, color 0.15s, border-color 0.15s;
    }
    .law-suggestion-btn:hover {
      background: #f1f5f9; color: #1e293b; border-color: #94a3b8;
    }

    /* Typing indicator */
    .law-typing-row { display: flex; align-items: flex-end; gap: 8px; align-self: flex-start; }
    .law-typing {
      display: flex; gap: 4px; align-items: center;
      padding: 10px 14px; background: #fff;
      border-radius: 14px; border-bottom-left-radius: 4px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    }
    .law-typing span {
      width: 7px; height: 7px; background: #94a3b8;
      border-radius: 50%; animation: law-bounce 1.2s infinite;
    }
    .law-typing span:nth-child(2) { animation-delay: 0.2s; }
    .law-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes law-bounce {
      0%, 80%, 100% { transform: translateY(0); }
      40% { transform: translateY(-6px); }
    }

    /* Input row */
    #law-chat-input-row {
      display: flex; gap: 8px; padding: 12px;
      background: #fff; border-top: 1px solid #e2e8f0; flex-shrink: 0;
    }
    #law-chat-input-row.law-input-hidden { display: none; }
    #law-chat-input {
      flex: 1; border: 1px solid #e2e8f0; border-radius: 10px;
      padding: 9px 13px; font-size: 16px; outline: none;
      resize: none; font-family: inherit; line-height: 1.4;
      max-height: 90px; overflow-y: auto;
    }
    #law-chat-input:focus { border-color: ${PRIMARY_COLOR}; }
    #law-chat-send {
      background: ${PRIMARY_COLOR}; color: #fff; border: none;
      border-radius: 10px; padding: 0 14px;
      cursor: pointer; font-size: 18px; flex-shrink: 0;
      transition: background 0.15s;
    }
    #law-chat-send:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Footer */
    #law-chat-footer {
      display: flex; align-items: center; justify-content: center;
      gap: 8px; padding: 4px 0 8px; background: #fff; flex-shrink: 0;
    }
    #law-powered { font-size: 11px; color: #94a3b8; }
    #law-reset-btn {
      background: none; border: none; color: #cbd5e1;
      font-size: 13px; cursor: pointer; padding: 0 4px;
      transition: color 0.15s; line-height: 1;
    }
    #law-reset-btn:hover { color: #94a3b8; }
    #law-reset-confirm {
      display: none; font-size: 11px; color: #ef4444;
      cursor: pointer; background: none; border: none;
      font-family: inherit; padding: 0 4px;
    }
    #law-reset-confirm:hover { text-decoration: underline; }

    @media (max-width: 500px) {
      #law-chat-window { bottom: 50px; }
    }
  `;
  document.head.appendChild(style);

  // ─── DOM ──────────────────────────────────────────────────────────────────
  const bubble = document.createElement("button");
  bubble.id = "law-chat-bubble";
  bubble.setAttribute("aria-label", "Open chat");
  if (LOGO_URL) {
    bubble.innerHTML = '<img src="' + LOGO_URL + '" alt="Chat">';
  } else {
    bubble.innerHTML = "\u2696\uFE0F"; // Balance scale emoji
  }

  const win = document.createElement("div");
  win.id = "law-chat-window";
  win.classList.add("law-hidden");
  win.innerHTML = `
    <div id="law-chat-header">
      <div class="law-header-icon">${LOGO_URL ? '<img src="' + LOGO_URL + '" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">' : "\u2696\uFE0F"}</div>
      <div id="law-chat-header-text">
        <h4>${FIRM_NAME}</h4>
        <p>${SUBTITLE}</p>
      </div>
      <button id="law-chat-close" aria-label="Close chat">\u2715</button>
    </div>
    <div id="law-prechat">
      <h3>Welcome</h3>
      <p>Please enter your email to get started. This helps us provide you with personalized assistance.</p>
      <input type="email" id="law-prechat-email" placeholder="Your email address *" required>
      <input type="text" id="law-prechat-name" placeholder="Your name (optional)">
      <button id="law-prechat-submit">Start Chat</button>
    </div>
    <div id="law-chat-messages"></div>
    <div id="law-chat-input-row" class="law-input-hidden">
      <textarea id="law-chat-input" rows="1" placeholder="Type a message..."></textarea>
      <button id="law-chat-send" aria-label="Send">\u27A4</button>
    </div>
    <div id="law-chat-footer">
      <span id="law-powered">Powered by AI Legal Assistant</span>
      <button id="law-reset-btn" title="Reset chat">\u21BB</button>
      <button id="law-reset-confirm">Reset chat?</button>
    </div>
  `;

  document.body.appendChild(bubble);
  document.body.appendChild(win);

  // ─── Element refs ─────────────────────────────────────────────────────────
  const prechatEl = win.querySelector("#law-prechat");
  const emailInput = win.querySelector("#law-prechat-email");
  const nameInput = win.querySelector("#law-prechat-name");
  const prechatSubmit = win.querySelector("#law-prechat-submit");
  const messagesEl = win.querySelector("#law-chat-messages");
  const inputRow = win.querySelector("#law-chat-input-row");
  const inputEl = win.querySelector("#law-chat-input");
  const sendBtn = win.querySelector("#law-chat-send");
  const resetBtn = win.querySelector("#law-reset-btn");
  const resetConfirm = win.querySelector("#law-reset-confirm");

  // ─── UI helpers ───────────────────────────────────────────────────────────
  function showChatUI() {
    prechatEl.classList.add("law-form-hidden");
    inputRow.classList.remove("law-input-hidden");
  }

  function addMessage(role, text) {
    if (role === "user") {
      const div = document.createElement("div");
      div.className = "law-msg law-msg-user";
      div.textContent = text;
      messagesEl.appendChild(div);
    } else {
      const row = document.createElement("div");
      row.className = "law-msg-row";
      row.innerHTML = '<div class="law-msg-avatar">AI</div>';
      const msgBubble = document.createElement("div");
      msgBubble.className = "law-msg law-msg-bot";
      msgBubble.textContent = text;
      row.appendChild(msgBubble);
      messagesEl.appendChild(row);
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addSuggestions(options) {
    if (!options || options.length === 0) return;
    const container = document.createElement("div");
    container.className = "law-suggestions";
    options.forEach(function (opt) {
      const btn = document.createElement("button");
      btn.className = "law-suggestion-btn";
      btn.textContent = opt;
      btn.addEventListener("click", function () {
        container.remove();
        sendMessage(opt);
      });
      container.appendChild(btn);
    });
    messagesEl.appendChild(container);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function showTyping() {
    const row = document.createElement("div");
    row.className = "law-typing-row";
    row.id = "law-typing-indicator";
    row.innerHTML = '<div class="law-msg-avatar">AI</div><div class="law-typing"><span></span><span></span><span></span></div>';
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function hideTyping() {
    var el = document.getElementById("law-typing-indicator");
    if (el) el.remove();
  }

  // ─── Pre-chat form submission ─────────────────────────────────────────────
  async function handlePrechat() {
    const email = emailInput.value.trim();
    if (!email) return;

    prechatSubmit.disabled = true;
    prechatSubmit.textContent = "Connecting...";

    try {
      const res = await fetch(LOOKUP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email,
          first_name: nameInput.value.trim() || null,
          channel: "website",
        }),
      });
      const data = await res.json();

      clientInfo = data.client;
      conversationId = data.conversation_id;
      identified = true;

      showChatUI();
      saveSession();

      // Greet the user
      var greeting;
      if (clientInfo && !clientInfo.is_new && clientInfo.first_name) {
        greeting = "Welcome back, " + clientInfo.first_name + "! How can I help you today?";
      } else {
        greeting = "Hello! Welcome to " + FIRM_NAME + ". How can I assist you today?";
      }
      addMessage("bot", greeting);
      messages.push({ role: "assistant", content: greeting });

      // Show suggestions from server (first-time flow)
      if (data.active_cases && data.active_cases.length > 0) {
        addSuggestions(["Update on my case", "New matter", "Schedule a consultation", "General question"]);
      } else {
        // Will get suggestions from first chat response
      }

      saveSession();
      inputEl.focus();
    } catch (err) {
      addMessage("bot", "Sorry, I couldn't connect to our system. Please try again.");
      prechatSubmit.disabled = false;
      prechatSubmit.textContent = "Start Chat";
    }
  }

  prechatSubmit.addEventListener("click", handlePrechat);
  emailInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); handlePrechat(); }
  });

  // ─── Send message ─────────────────────────────────────────────────────────
  async function sendMessage(text) {
    if (!text.trim() || isWaiting || !identified) return;

    // Remove suggestion buttons
    messagesEl.querySelectorAll(".law-suggestions").forEach(function (el) { el.remove(); });

    addMessage("user", text.trim());
    messages.push({ role: "user", content: text.trim() });
    saveSession();
    inputEl.value = "";
    inputEl.style.height = "auto";
    isWaiting = true;
    sendBtn.disabled = true;
    showTyping();

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: conversationId,
          client_email: clientInfo.email,
          message: text.trim(),
          practice_area: PRACTICE_AREA,
        }),
      });
      const data = await res.json();
      hideTyping();

      // Update conversation ID if it changed
      if (data.conversation_id) conversationId = data.conversation_id;

      var reply = data.reply || "Sorry, something went wrong. Please try again!";
      addMessage("bot", reply);
      messages.push({ role: "assistant", content: reply });
      saveSession();

      // Server-driven suggestions
      if (data.suggestions) {
        addSuggestions(data.suggestions);
      }
    } catch (err) {
      hideTyping();
      addMessage("bot", "Sorry, I couldn't connect. Please check your internet and try again.");
    }

    isWaiting = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }

  // ─── Restore session ──────────────────────────────────────────────────────
  function restoreSession(saved) {
    clientInfo = saved.clientInfo;
    conversationId = saved.conversationId;
    identified = saved.identified;

    if (identified) {
      showChatUI();
    }

    if (saved.messages) {
      saved.messages.forEach(function (msg) {
        messages.push(msg);
        addMessage(msg.role === "user" ? "user" : "bot", msg.content);
      });
    }
  }

  // ─── Reset ────────────────────────────────────────────────────────────────
  function resetChat() {
    messages.length = 0;
    messagesEl.innerHTML = "";
    clientInfo = null;
    conversationId = null;
    identified = false;
    clearSession();
    resetConfirm.style.display = "none";
    resetBtn.style.display = "";
    // Show pre-chat form again
    prechatEl.classList.remove("law-form-hidden");
    inputRow.classList.add("law-input-hidden");
    emailInput.value = "";
    nameInput.value = "";
    prechatSubmit.disabled = false;
    prechatSubmit.textContent = "Start Chat";
  }

  resetBtn.addEventListener("click", function () {
    resetBtn.style.display = "none";
    resetConfirm.style.display = "inline";
    setTimeout(function () {
      resetConfirm.style.display = "none";
      resetBtn.style.display = "";
    }, 3000);
  });
  resetConfirm.addEventListener("click", resetChat);

  // ─── Open / Close ─────────────────────────────────────────────────────────
  function openChat() {
    isOpen = true;
    win.classList.remove("law-hidden");
    bubble.innerHTML = '<span style="font-size:28px;color:#fff;">\u2715</span>';

    if (!identified) {
      // Check for saved session
      var saved = loadSession();
      if (saved && saved.identified) {
        restoreSession(saved);
      }
      // Otherwise show the pre-chat form
    }

    if (identified) inputEl.focus();
    else emailInput.focus();
  }

  function closeChat() {
    isOpen = false;
    win.classList.add("law-hidden");
    if (LOGO_URL) {
      bubble.innerHTML = '<img src="' + LOGO_URL + '" alt="Chat">';
    } else {
      bubble.innerHTML = "\u2696\uFE0F";
    }
  }

  bubble.addEventListener("click", function () { isOpen ? closeChat() : openChat(); });
  win.querySelector("#law-chat-close").addEventListener("click", closeChat);
  sendBtn.addEventListener("click", function () { sendMessage(inputEl.value); });

  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(inputEl.value);
    }
  });

  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 90) + "px";
  });
})();
