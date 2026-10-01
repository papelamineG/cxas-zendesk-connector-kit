/*!
 * cxas-zendesk-bridge.js — CX Agent Studio <chat-messenger> <-> Zendesk live hand-off.
 *
 *   <link rel="stylesheet" href="cxas-zendesk-bridge.css">
 *   <script src="cxas-zendesk-bridge.js"></script>
 *   <script>
 *     CxasZendeskBridge.init({
 *       apiBase: 'https://your-relay.run.app',      // '' = same origin
 *       language: 'en',                              // en | es | fr | de
 *       getCustomer: () => ({ name: user.name, email: user.email }),
 *     });
 *   </script>
 *
 * What it does
 *   1. Registers the `escalate_to_human` client function on
 *      messenger.presenter.registerClientSideFunctionForAllTools (name-only
 *      registry, immune to tool-path / project-number mismatches) with a bounded
 *      retry, because `chat-messenger-loaded` may fire before this script runs.
 *   2. When the agent calls it, opens a Zendesk ticket through the relay
 *      (POST /api/escalation/start). Customer identity comes from getCustomer(),
 *      NEVER from tool arguments, so the model cannot invent a requester.
 *   3. Mounts an opaque overlay composer over the widget's bottom input band
 *      (the widget input lives in a closed shadow root: it can only be covered).
 *   4. Polls GET /api/escalation/{id}/messages?since={cursor} every 2s and only
 *      advances the cursor after a message was actually rendered.
 *   5. Forwards customer replies (POST /api/escalation/{id}/reply).
 *   6. On ticket solved/closed, removes the overlay and hands back to the AI.
 *
 * Works with the real <chat-messenger> (renderCustomText) and, out of the box,
 * with the bundled <cxas-preview-messenger> offline preview widget.
 */
(function (global) {
  'use strict';

  var LOG = '[cxas-zendesk]';
  var VERSION = '1.0.0';
  var STORAGE_KEY = 'cxas_zendesk_escalation';
  var TERMINAL = ['solved', 'closed'];

  // ------------------------------------------------------------------------
  // i18n — strings may use {who} / {body}, or be functions (who, body) => str
  // ------------------------------------------------------------------------
  var DEFAULT_COPY = {
    en: {
      waiting: 'Connecting you with a specialist…',
      reconnecting: 'Reconnecting to the specialist…',
      joined: '👤 {who} has joined the conversation',
      online: '{who} · online',
      message: '**{who}:** {body}',
      timeout: "Our specialists are all busy right now. We'll follow up by email shortly.",
      lost: "The connection to our specialist was interrupted. We'll follow up by email shortly.",
      handback: '{who} has resolved your case. Can I help you with anything else?',
      placeholder: 'Message the specialist…',
      send: 'Send',
      sending: '…',
      composerLabel: 'Message for the specialist',
      regionLabel: 'Live specialist chat',
      sendFailed: 'Message not delivered — please try again.',
      defaultSummary: 'The customer asked to speak to a person.',
      specialistFallback: 'The specialist'
    },
    es: {
      waiting: 'Conectando con un especialista…',
      reconnecting: 'Reconectando con el especialista…',
      joined: '👤 {who} se ha unido a la conversación',
      online: '{who} · en línea',
      message: '**{who}:** {body}',
      timeout: 'Nuestros especialistas están ocupados en este momento. Te contactaremos por email en breve.',
      lost: 'Se ha interrumpido la conexión con el especialista. Te contactaremos por email en breve.',
      handback: '{who} ha resuelto tu incidencia. ¿Puedo ayudarte con algo más?',
      placeholder: 'Escribe al especialista…',
      send: 'Enviar',
      sending: '…',
      composerLabel: 'Mensaje para el especialista',
      regionLabel: 'Chat en vivo con un especialista',
      sendFailed: 'Mensaje no entregado — inténtalo de nuevo.',
      defaultSummary: 'El cliente ha pedido hablar con una persona.',
      specialistFallback: 'El especialista'
    },
    fr: {
      waiting: 'Mise en relation avec un conseiller…',
      reconnecting: 'Reconnexion au conseiller…',
      joined: '👤 {who} a rejoint la conversation',
      online: '{who} · en ligne',
      message: '**{who} :** {body}',
      timeout: 'Nos conseillers sont tous occupés. Nous vous recontacterons très vite par e-mail.',
      lost: 'La connexion avec le conseiller a été interrompue. Nous vous recontacterons très vite par e-mail.',
      handback: '{who} a résolu votre demande. Puis-je vous aider pour autre chose ?',
      placeholder: 'Écrire au conseiller…',
      send: 'Envoyer',
      sending: '…',
      composerLabel: 'Message pour le conseiller',
      regionLabel: 'Discussion en direct avec un conseiller',
      sendFailed: 'Message non remis — veuillez réessayer.',
      defaultSummary: 'Le client a demandé à parler à une personne.',
      specialistFallback: 'Le conseiller'
    },
    de: {
      waiting: 'Sie werden mit einem Mitarbeiter verbunden…',
      reconnecting: 'Verbindung zum Mitarbeiter wird wiederhergestellt…',
      joined: '👤 {who} ist dem Gespräch beigetreten',
      online: '{who} · online',
      message: '**{who}:** {body}',
      timeout: 'Unsere Mitarbeiter sind gerade alle beschäftigt. Wir melden uns in Kürze per E-Mail.',
      lost: 'Die Verbindung zum Mitarbeiter wurde unterbrochen. Wir melden uns in Kürze per E-Mail.',
      handback: '{who} hat Ihr Anliegen gelöst. Kann ich Ihnen sonst noch helfen?',
      placeholder: 'Nachricht an den Mitarbeiter…',
      send: 'Senden',
      sending: '…',
      composerLabel: 'Nachricht für den Mitarbeiter',
      regionLabel: 'Live-Chat mit einem Mitarbeiter',
      sendFailed: 'Nachricht nicht zugestellt — bitte erneut versuchen.',
      defaultSummary: 'Der Kunde möchte mit einer Person sprechen.',
      specialistFallback: 'Der Mitarbeiter'
    }
  };

  var DEFAULTS = {
    apiBase: null,                    // null -> window.CXAS_ZENDESK_API_BASE -> '' (same origin)
    language: 'en',
    defaultLanguage: 'en',
    messengerSelector: 'chat-messenger, cxas-preview-messenger',
    toolName: 'escalate_to_human',
    pollIntervalMs: 2000,
    noReplyTimeoutMs: 5 * 60 * 1000,  // nobody picked up -> tell the customer
    hardStopMs: 15 * 60 * 1000,       // leak protection for a forgotten poll
    composerHeight: 106,              // measured height of <chat-messenger>'s input band
    registerAttempts: 40,
    registerIntervalMs: 250,
    persist: true,                    // survive an accidental refresh (sessionStorage)
    getCustomer: function () { return { name: 'Valued Customer', email: 'customer@example.com' }; },
    copy: {},
    fallbackTranscript: null,         // element / selector used when no messenger can render
    onStateChange: null
  };

  var opts = Object.assign({}, DEFAULTS);
  var initialized = false;
  var registeredMessengers = typeof WeakSet === 'function' ? new WeakSet() : null;
  var pollTimer = null;
  var pollInFlight = false;
  var overlay = null;
  var placeRaf = null;
  var focusTarget = null;

  var session = freshSession();

  function freshSession() {
    return {
      escalationId: null,
      active: false,
      language: opts.language || 'en',
      specialistJoined: false,
      specialistName: null,
      cursor: 0,                 // advances only after a successful render
      startedAt: 0,
      ticketStatus: null,
      lastError: null,
      endedReason: null,
      endedAt: null
    };
  }

  // ------------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------------
  function normalizeLanguage(code) {
    var short = String(code || '').toLowerCase().slice(0, 2);
    return DEFAULT_COPY[short] || (opts.copy && opts.copy[short]) ? short : (opts.defaultLanguage || 'en');
  }

  function copyFor(language) {
    var lang = normalizeLanguage(language);
    var base = DEFAULT_COPY[lang] || DEFAULT_COPY.en;
    var custom = (opts.copy && opts.copy[lang]) || {};
    return Object.assign({}, DEFAULT_COPY.en, base, custom);
  }

  function fmt(template, who, body) {
    if (typeof template === 'function') return template(who, body);
    return String(template).replace(/\{(who|body)\}/g, function (_, key) {
      return key === 'who' ? (who || '') : (body || '');
    });
  }

  function apiBase() {
    if (opts.apiBase !== null && opts.apiBase !== undefined) return String(opts.apiBase).replace(/\/$/, '');
    if (global.CXAS_ZENDESK_API_BASE) return String(global.CXAS_ZENDESK_API_BASE).replace(/\/$/, '');
    return '';
  }

  function findMessenger() {
    var selectors = String(opts.messengerSelector || '').split(',');
    for (var i = 0; i < selectors.length; i++) {
      var sel = selectors[i].trim();
      if (!sel) continue;
      var el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function emit() {
    var state = getState();
    try { if (typeof opts.onStateChange === 'function') opts.onStateChange(state); } catch (e) { console.error(LOG, e); }
    try { global.dispatchEvent(new CustomEvent('cxas-zendesk:state', { detail: state })); } catch (e) { /* old browsers */ }
  }

  function persist() {
    if (!opts.persist) return;
    try {
      if (!session.active || !session.escalationId) { sessionStorage.removeItem(STORAGE_KEY); return; }
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        escalationId: session.escalationId, language: session.language,
        specialistJoined: session.specialistJoined, specialistName: session.specialistName,
        cursor: session.cursor, startedAt: session.startedAt
      }));
    } catch (e) { /* storage disabled: the escalation still works in this tab */ }
  }

  async function postJson(path, body) {
    var response = await fetch(apiBase() + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    });
    var data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    return { ok: response.ok, data: data };
  }

  // ------------------------------------------------------------------------
  // Rendering — real <chat-messenger>, preview widget, or fallback transcript
  // ------------------------------------------------------------------------
  /** Returns true only when the text actually reached a transcript. */
  function render(text, meta) {
    meta = meta || {};
    var isAgent = meta.isAgent !== false;
    var messenger = findMessenger();
    if (messenger && typeof messenger.renderCustomText === 'function') {
      try {
        if (messenger.isCxasPreview) messenger.renderCustomText(text, isAgent, meta);
        else if (isAgent) messenger.renderCustomText(text);
        else messenger.renderCustomText(text, false);   // renders as the USER's own turn
        return true;
      } catch (err) {
        console.error(LOG, 'renderCustomText failed', err);
        return false;
      }
    }
    var fallback = typeof opts.fallbackTranscript === 'string'
      ? document.querySelector(opts.fallbackTranscript) : opts.fallbackTranscript;
    if (fallback && fallback.appendChild) {
      var line = document.createElement('div');
      line.className = 'cxas-zd-fallback-line';
      line.setAttribute('data-role', meta.role || (isAgent ? 'assistant' : 'user'));
      line.textContent = text;
      fallback.appendChild(line);
      return true;
    }
    console.error(LOG, 'cannot render — no messenger with renderCustomText and no fallbackTranscript');
    return false;
  }

  // ------------------------------------------------------------------------
  // Client-function registration (race-safe)
  // ------------------------------------------------------------------------
  function registerOn(messenger) {
    var registry = messenger && messenger.presenter;
    if (!registry || typeof registry.registerClientSideFunctionForAllTools !== 'function') return false;
    if (registeredMessengers && registeredMessengers.has(messenger)) return true;
    if (!registeredMessengers && messenger.__cxasZendeskRegistered) return true;
    registry.registerClientSideFunctionForAllTools(opts.toolName, escalationHandler);
    if (registeredMessengers) registeredMessengers.add(messenger); else messenger.__cxasZendeskRegistered = true;
    console.info(LOG, "client function '" + opts.toolName + "' registered on", messenger.tagName.toLowerCase());
    return true;
  }

  function registerWhenReady(attempts) {
    var any = false;
    var nodes = document.querySelectorAll(opts.messengerSelector);
    for (var i = 0; i < nodes.length; i++) { if (registerOn(nodes[i])) any = true; }
    if (any) return;
    if (attempts <= 0) {
      console.warn(LOG, 'gave up registering ' + opts.toolName + ' — is <chat-messenger> on the page?');
      return;
    }
    setTimeout(function () { registerWhenReady(attempts - 1); }, opts.registerIntervalMs);
  }

  // ------------------------------------------------------------------------
  // Escalation lifecycle
  // ------------------------------------------------------------------------
  // Must resolve quickly: a synchronous client function blocks the CXAS session
  // (server-side Tool.timeout, 30 s by default). Create the ticket and return;
  // polling runs afterwards.
  async function escalationHandler(args) {
    args = args || {};
    if (session.active && session.escalationId) {
      return { status: 'SUCCESS', ticket_id: String(session.escalationId), already_active: true };
    }
    var language = normalizeLanguage(args.language || opts.language);
    var copy = copyFor(language);
    var customer = {};
    try { customer = (await opts.getCustomer()) || {}; } catch (e) { customer = {}; }
    try {
      var result = await postJson('/api/escalation/start', {
        summary: args.summary || copy.defaultSummary,
        order_id: args.order_id || undefined,
        language: language,
        customer_name: customer.name || 'Valued Customer',
        customer_email: customer.email || 'customer@example.com'
      });
      var data = result.data || {};
      // Zero silent failure: never report a ticket the relay did not create.
      if (!result.ok || data.status !== 'SUCCESS' || !data.ticket_id) {
        session.lastError = data.error_code || 'ESCALATION_FAILED';
        console.error(LOG, 'escalation failed:', data);
        emit();
        return {
          status: 'ERROR',
          error_code: data.error_code || 'ESCALATION_FAILED',
          agent_action: data.agent_action ||
            'Tell the customer you could not reach a specialist right now and offer to follow up by email.'
        };
      }
      var id = String(data.ticket_id);
      console.info(LOG, 'escalation ticket opened: ' + id);
      startSession(id, language, null);
      return { status: 'SUCCESS', ticket_id: id };
    } catch (err) {
      console.error(LOG, 'relay unreachable:', err);
      session.lastError = 'RELAY_UNREACHABLE';
      emit();
      return {
        status: 'ERROR', error_code: 'RELAY_UNREACHABLE',
        agent_action: 'Tell the customer you could not reach a specialist right now and offer to follow up by email.'
      };
    }
  }

  function startSession(id, language, restored) {
    stopPolling();
    session = freshSession();
    session.escalationId = id;
    session.active = true;
    session.language = language;
    session.startedAt = (restored && restored.startedAt) || Date.now();
    if (restored) {
      session.cursor = restored.cursor || 0;
      session.specialistJoined = !!restored.specialistJoined;
      session.specialistName = restored.specialistName || null;
    }
    persist();
    mountOverlay();
    var copy = copyFor(language);
    if (session.specialistJoined && session.specialistName) setPill('joined', fmt(copy.online, session.specialistName));
    else setPill('waiting', copy.waiting);
    emit();
    pollOnce().then(schedulePoll);   // don't make the customer wait a full tick
  }

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function schedulePoll() {
    stopPolling();
    if (!session.active) return;
    pollTimer = setTimeout(async function () {
      if (!session.active) return;
      if (Date.now() - session.startedAt > opts.hardStopMs) {
        console.warn(LOG, 'poll hard stop reached');
        handBackToAssistant('timeout');
        return;
      }
      await pollOnce();
      schedulePoll();
    }, opts.pollIntervalMs);
  }

  async function pollOnce() {
    if (!session.active || !session.escalationId || pollInFlight) return;
    pollInFlight = true;
    var id = session.escalationId;
    var copy = copyFor(session.language);
    var payload;
    try {
      var query = 'since=' + encodeURIComponent(session.cursor) + '&language=' + encodeURIComponent(session.language);
      var response = await fetch(apiBase() + '/api/escalation/' + encodeURIComponent(id) + '/messages?' + query);
      payload = await response.json();
    } catch (err) {
      // Survivable: the cursor has not moved, the next tick asks again.
      console.warn(LOG, 'poll failed, will retry:', err);
      setPill('error', copy.reconnecting);
      pollInFlight = false;
      return;
    }
    try {
      if (!session.active || session.escalationId !== id) return;
      if (!payload || payload.status === 'ERROR') {
        console.error(LOG, 'poll error:', payload && payload.error_code, payload && payload.message);
        if (payload && payload.error_code === 'UNKNOWN_ESCALATION') handBackToAssistant('lost');
        else setPill('error', copy.reconnecting);
        return;
      }

      if (!session.specialistJoined && Date.now() - session.startedAt > opts.noReplyTimeoutMs) {
        console.info(LOG, 'no specialist replied within the timeout');
        handBackToAssistant('timeout');
        return;
      }

      var messages = payload.messages || [];
      for (var i = 0; i < messages.length; i++) {
        var message = messages[i];
        var who = message.author_name || copy.specialistFallback;
        if (!session.specialistJoined) {
          if (!render(fmt(copy.joined, who), { role: 'system' })) return;   // retry next tick
          session.specialistJoined = true;
          console.info(LOG, 'specialist joined: ' + who);
        }
        // Only acknowledge what actually reached the transcript.
        if (!render(fmt(copy.message, who, message.body), { role: 'specialist', author: who })) return;
        session.cursor = Math.max(session.cursor, Number(message.id) || 0);
        session.specialistName = who;
        persist();
        console.info(LOG, 'specialist message rendered');
      }

      if (session.specialistJoined && session.specialistName) setPill('joined', fmt(copy.online, session.specialistName));
      else setPill('waiting', copy.waiting);

      session.ticketStatus = payload.ticket_status || session.ticketStatus;
      emit();

      // Checked last, so a final message sent with the resolution is delivered first.
      if (TERMINAL.indexOf(payload.ticket_status) !== -1) {
        console.info(LOG, 'ticket ' + payload.ticket_status + ' by the specialist');
        handBackToAssistant(payload.ticket_status);
      }
    } finally {
      pollInFlight = false;
    }
  }

  /** Send what the customer typed to the specialist. Resolves to whether it landed. */
  async function deliverCustomerReply(text) {
    var body = String(text || '').trim();
    if (!body || !session.active || !session.escalationId) return false;
    var copy = copyFor(session.language);
    try {
      var result = await postJson('/api/escalation/' + encodeURIComponent(session.escalationId) + '/reply', { body: body });
      if (!result.ok || !result.data || result.data.status !== 'SUCCESS') {
        console.error(LOG, 'reply not delivered:', result.data && result.data.error_code);
        setPill('error', copy.sendFailed);
        return false;
      }
      // Echo as the customer's own turn so the thread reads as one conversation.
      render(body, { isAgent: false, role: 'user' });
      console.info(LOG, 'customer reply delivered');
      return true;
    } catch (err) {
      console.error(LOG, 'reply relay unreachable:', err);
      setPill('error', copy.sendFailed);
      return false;
    }
  }

  /** Return the conversation to the AI assistant (the CXAS session never ended). */
  function handBackToAssistant(reason) {
    reason = reason || 'manual';
    var copy = copyFor(session.language);
    var who = session.specialistName || copy.specialistFallback;
    stopPolling();
    unmountOverlay();
    var text = reason === 'timeout' ? copy.timeout
      : reason === 'lost' ? copy.lost
      : fmt(copy.handback, who);
    var endedId = session.escalationId;
    session.active = false;
    session.escalationId = null;
    session.endedReason = reason;
    session.endedAt = Date.now();
    persist();
    render(text, { role: 'assistant' });
    console.info(LOG, 'handed back to the assistant (' + reason + ', ticket ' + endedId + ')');
    emit();
  }

  function getState() {
    return Object.assign({}, session, { overlayMounted: !!overlay, version: VERSION });
  }

  // ------------------------------------------------------------------------
  // Overlay composer
  // ------------------------------------------------------------------------
  function setPill(state, text) {
    if (!overlay) return;
    var pill = overlay.querySelector('.cxas-zd-pill');
    pill.setAttribute('data-state', state);
    overlay.querySelector('.cxas-zd-pill-text').textContent = text;
  }

  function positionOverlay() {
    if (!overlay) return;
    var messenger = findMessenger();
    var rect = messenger ? messenger.getBoundingClientRect() : null;
    var visible = rect && rect.width > 0 && rect.height > 0 &&
      getComputedStyle(messenger).visibility !== 'hidden';
    if (!visible) {
      overlay.hidden = true;
      return;
    }
    var h = Math.min(opts.composerHeight, rect.height);
    // A floating (fixed) widget gets a fixed mask. An in-flow widget gets a mask
    // in document coordinates, so page scrolling moves both together natively
    // instead of leaving the widget input exposed for a frame.
    var fixed = isFixedPositioned(messenger);
    var dx = fixed ? 0 : global.scrollX;
    var dy = fixed ? 0 : global.scrollY;
    overlay.hidden = false;
    overlay.style.position = fixed ? 'fixed' : 'absolute';
    overlay.style.left = (rect.left + dx) + 'px';
    overlay.style.top = (rect.bottom - h + dy) + 'px';
    overlay.style.width = rect.width + 'px';
    overlay.style.height = h + 'px';
  }

  function isFixedPositioned(el) {
    for (var node = el; node && node.nodeType === 1; node = node.parentElement) {
      var position = getComputedStyle(node).position;
      if (position === 'fixed' || position === 'sticky') return true;
    }
    return false;
  }

  // Re-measured every frame (layout changes, widget open/close) AND synchronously
  // on scroll/resize, so the mask never lags a frame behind a scrolling page.
  function placeOverlay() {
    positionOverlay();
    if (overlay) placeRaf = global.requestAnimationFrame(placeOverlay);
  }

  function redirectFocus(event) {
    // Keyboard users must not reach the covered widget input either.
    if (!overlay || overlay.hidden) return;
    var input = overlay.querySelector('.cxas-zd-input');
    if (event.target !== input) input.focus();
  }

  function mountOverlay() {
    if (overlay) return;
    var copy = copyFor(session.language);
    overlay = document.createElement('div');
    overlay.className = 'cxas-zd-composer';
    overlay.setAttribute('role', 'region');
    overlay.setAttribute('aria-label', copy.regionLabel);
    overlay.style.setProperty('--cxas-zd-composer-height', opts.composerHeight + 'px');

    var pill = document.createElement('div');
    pill.className = 'cxas-zd-pill';
    pill.setAttribute('data-state', 'waiting');
    pill.setAttribute('role', 'status');
    pill.setAttribute('aria-live', 'polite');
    var pulse = document.createElement('span');
    pulse.className = 'cxas-zd-pulse';
    pulse.setAttribute('aria-hidden', 'true');
    var pillText = document.createElement('span');
    pillText.className = 'cxas-zd-pill-text';
    pillText.textContent = copy.waiting;
    pill.appendChild(pulse);
    pill.appendChild(pillText);

    var form = document.createElement('form');
    form.className = 'cxas-zd-row';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'cxas-zd-input';
    input.placeholder = copy.placeholder;
    input.autocomplete = 'off';
    input.setAttribute('aria-label', copy.composerLabel);
    var button = document.createElement('button');
    button.type = 'submit';
    button.className = 'cxas-zd-send';
    button.textContent = copy.send;
    button.disabled = true;
    form.appendChild(input);
    form.appendChild(button);

    var sending = false;
    input.addEventListener('input', function () { button.disabled = sending || !input.value.trim(); });
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var text = input.value.trim();
      if (!text || sending) return;
      sending = true;
      button.disabled = true;
      button.textContent = copy.sending;
      var ok = await deliverCustomerReply(text);
      sending = false;
      button.textContent = copy.send;
      if (ok) input.value = '';
      button.disabled = !input.value.trim();
      if (overlay) input.focus();
    });

    overlay.appendChild(pill);
    overlay.appendChild(form);
    document.body.appendChild(overlay);

    var messenger = findMessenger();
    if (messenger) {
      messenger.classList.add('cxas-zd-escalated');
      messenger.addEventListener('focusin', redirectFocus);
      focusTarget = messenger;
    }
    global.addEventListener('scroll', positionOverlay, true);
    global.addEventListener('resize', positionOverlay);
    placeOverlay();
    try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); }
  }

  function unmountOverlay() {
    if (placeRaf) { global.cancelAnimationFrame(placeRaf); placeRaf = null; }
    global.removeEventListener('scroll', positionOverlay, true);
    global.removeEventListener('resize', positionOverlay);
    if (focusTarget) {
      focusTarget.removeEventListener('focusin', redirectFocus);
      focusTarget.classList.remove('cxas-zd-escalated');
      focusTarget = null;
    }
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    overlay = null;
  }

  // ------------------------------------------------------------------------
  // <cxas-preview-messenger> — offline, <chat-messenger>-compatible preview.
  // Implements the surface the bridge relies on (presenter registry +
  // renderCustomText) plus a tiny scripted assistant, so the full hand-off can
  // be tested before a CXAS app exists.
  // ------------------------------------------------------------------------
  var ESCALATION_INTENT = /\b(human|person|agent|specialist|representative|someone real|real person|humano|persona|especialista|personne|conseiller|humain|mensch|mitarbeiter|berater)\b/i;

  function appendRich(parent, text) {
    // Minimal, XSS-safe **bold** support; everything else is text.
    var parts = String(text).split(/(\*\*[^*]+\*\*)/g);
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      if (!part) continue;
      if (/^\*\*[^*]+\*\*$/.test(part)) {
        var strong = document.createElement('strong');
        strong.textContent = part.slice(2, -2);
        parent.appendChild(strong);
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    }
  }

  function definePreviewMessenger() {
    if (!global.customElements || global.customElements.get('cxas-preview-messenger')) return;

    class CxasPreviewMessenger extends HTMLElement {
      constructor() {
        super();
        var fns = new Map();
        this._fns = fns;
        this.isCxasPreview = true;
        // Same registry shape as <chat-messenger>.presenter.
        this.presenter = {
          registerClientSideFunctionForAllTools: function (name, handler) { fns.set(name, handler); },
          registerClientSideFunction: function (_path, name, handler) { fns.set(name, handler); }
        };
      }

      connectedCallback() {
        if (this._built) return;
        this._built = true;
        var title = this.getAttribute('chat-title') || 'AI Assistant (preview)';
        this.innerHTML = '';
        var header = document.createElement('div');
        header.className = 'cxas-preview-header';
        var avatar = document.createElement('div');
        avatar.className = 'cxas-preview-avatar';
        avatar.textContent = 'AI';
        avatar.setAttribute('aria-hidden', 'true');
        var titles = document.createElement('div');
        var t1 = document.createElement('div');
        t1.className = 'cxas-preview-title';
        t1.textContent = title;
        var t2 = document.createElement('div');
        t2.className = 'cxas-preview-subtitle';
        t2.textContent = 'Offline preview · <chat-messenger> compatible';
        titles.appendChild(t1);
        titles.appendChild(t2);
        header.appendChild(avatar);
        header.appendChild(titles);

        this._transcript = document.createElement('div');
        this._transcript.className = 'cxas-preview-transcript';
        this._transcript.setAttribute('role', 'log');
        this._transcript.setAttribute('aria-live', 'polite');
        this._transcript.setAttribute('aria-label', 'Chat transcript');

        var form = document.createElement('form');
        form.className = 'cxas-preview-composer';
        this._input = document.createElement('input');
        this._input.type = 'text';
        this._input.className = 'cxas-preview-input';
        this._input.placeholder = 'Type a message…';
        this._input.setAttribute('aria-label', 'Message the assistant');
        var send = document.createElement('button');
        send.type = 'submit';
        send.className = 'cxas-preview-send';
        send.textContent = 'Send';
        send.setAttribute('aria-label', 'Send');
        form.appendChild(this._input);
        form.appendChild(send);
        var self = this;
        form.addEventListener('submit', function (event) {
          event.preventDefault();
          var text = self._input.value.trim();
          if (!text) return;
          self._input.value = '';
          self.sendUserMessage(text);
        });

        this.appendChild(header);
        this.appendChild(this._transcript);
        this.appendChild(form);

        this.renderCustomText(this.getAttribute('greeting') ||
          "Hi! I'm the offline preview assistant. Ask me anything — or type “I want to talk to a human” to test the live Zendesk hand-off.");
        global.dispatchEvent(new CustomEvent('cxas-preview-messenger-loaded', { detail: { element: this } }));
      }

      /** <chat-messenger>-compatible: renderCustomText(text, isAgent = true). */
      renderCustomText(text, isAgent, meta) {
        meta = meta || {};
        var role = meta.role || (isAgent === false ? 'user' : 'assistant');
        var bubble = document.createElement('div');
        bubble.className = 'cxas-preview-msg';
        bubble.setAttribute('data-role', role);
        if (role === 'specialist') {
          var badge = document.createElement('div');
          badge.className = 'cxas-preview-badge';
          badge.textContent = 'Human specialist';
          bubble.appendChild(badge);
          bubble.appendChild(document.createElement('br'));
          if (meta.author) bubble.setAttribute('data-author', meta.author);
        }
        appendRich(bubble, text);
        this._transcript.appendChild(bubble);
        this._transcript.scrollTop = this._transcript.scrollHeight;
        return bubble;
      }

      getTranscript() {
        return Array.prototype.map.call(this._transcript.querySelectorAll('.cxas-preview-msg'), function (el) {
          return { role: el.getAttribute('data-role'), text: el.textContent };
        });
      }

      async sendUserMessage(text) {
        if (this.classList.contains('cxas-zd-escalated')) return;   // a human has the conversation
        this.renderCustomText(text, false);
        var self = this;
        await new Promise(function (resolve) { setTimeout(resolve, 350); });
        if (ESCALATION_INTENT.test(text)) {
          var handler = this._fns.get('escalate_to_human');
          if (!handler) {
            self.renderCustomText('(No escalate_to_human client function is registered on this widget.)');
            return;
          }
          self.renderCustomText('Of course — let me connect you with a specialist.');
          var result;
          try {
            result = await handler({
              summary: 'Customer asked for a human: "' + text.slice(0, 200) + '"',
              language: this.getAttribute('language') || opts.language || 'en'
            });
          } catch (err) {
            result = { status: 'ERROR', agent_action: String(err) };
          }
          if (!result || result.status !== 'SUCCESS') {
            self.renderCustomText("I'm sorry — I couldn't reach a specialist right now. We'll follow up by email.");
          } else {
            self.renderCustomText("I've shared our conversation with the support team. A specialist will join right here shortly.");
          }
          return;
        }
        self.renderCustomText('(Preview assistant) You said: “' + text + '”. Connect your real CX Agent Studio app ' +
          'from the configuration bar for real answers, or ask for a human to test the hand-off.');
      }
    }

    global.customElements.define('cxas-preview-messenger', CxasPreviewMessenger);
  }

  // ------------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------------
  function restoreFromStorage() {
    if (!opts.persist) return;
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null'); } catch (e) { saved = null; }
    if (saved && saved.escalationId && !session.active) {
      console.info(LOG, 'resuming escalation ' + saved.escalationId);
      startSession(String(saved.escalationId), normalizeLanguage(saved.language), saved);
    }
  }

  function init(options) {
    opts = Object.assign({}, DEFAULTS, opts, options || {});
    opts.language = normalizeLanguage(opts.language);
    if (!session.active) session.language = opts.language;
    if (initialized) return api;
    initialized = true;
    definePreviewMessenger();
    var retry = function () { registerWhenReady(opts.registerAttempts); };
    global.addEventListener('chat-messenger-loaded', retry);
    global.addEventListener('cxas-preview-messenger-loaded', retry);
    retry();
    restoreFromStorage();
    emit();
    return api;
  }

  async function triggerEscalation(params) {
    return escalationHandler(params || {});
  }

  function setLanguage(code) {
    opts.language = normalizeLanguage(code);
    if (!session.active) session.language = opts.language;
    emit();
  }

  var api = {
    version: VERSION,
    init: init,
    triggerEscalation: triggerEscalation,
    deliverCustomerReply: deliverCustomerReply,
    handBackToAssistant: handBackToAssistant,
    getState: getState,
    setLanguage: setLanguage,
    definePreviewMessenger: definePreviewMessenger,
    copy: DEFAULT_COPY,
    _pollOnce: pollOnce
  };

  definePreviewMessenger();
  global.CxasZendeskBridge = api;
})(typeof window !== 'undefined' ? window : this);
