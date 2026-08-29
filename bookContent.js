// ==UserScript==
// @name         69shuba Content Auto-Sender
// @namespace    http://tampermonkey.net/
// @version      2.2
// @description  Scrapes novel chapter text from 69shuba and sends it to the translation server
// @author       Gemini
// @match        *://www.69shuba.com/txt/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {
    'use strict';

    const SERVER_URL = "http://127.0.0.1:5001";
    let isPaused = false;

    // ==========================================
    // 🖥️ STATUS PANEL UI
    // ==========================================
    const styles = `
        #gbp-panel {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 320px;
            background: #1a1a2e;
            border-radius: 12px;
            border: 0.5px solid rgba(255,255,255,0.1);
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 12px;
            z-index: 999999;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            display: flex;
            flex-direction: column;
            overflow: hidden;
            transition: height 0.2s ease;
        }
        #gbp-panel.minimized { height: 40px; }
        #gbp-header {
            background: #16213e;
            padding: 0 12px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
            cursor: move;
            border-bottom: 0.5px solid rgba(255,255,255,0.08);
            user-select: none;
        }
        #gbp-header-left { display: flex; align-items: center; gap: 8px; }
        #gbp-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #555; transition: background 0.3s, box-shadow 0.3s;
            flex-shrink: 0;
        }
        #gbp-dot.ok   { background: #1de9b6; box-shadow: 0 0 6px #1de9b6; }
        #gbp-dot.warn { background: #ffb700; box-shadow: 0 0 6px #ffb700; }
        #gbp-dot.err  { background: #ef5350; box-shadow: 0 0 6px #ef5350; }
        #gbp-title { color: #e0e0e0; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; }
        #gbp-header-right { display: flex; gap: 4px; }
        .gbp-btn {
            background: rgba(255,255,255,0.06);
            border: 0.5px solid rgba(255,255,255,0.1);
            border-radius: 4px;
            color: #888;
            font-size: 11px;
            padding: 1px 7px;
            cursor: pointer;
            font-family: inherit;
        }
        .gbp-btn:hover { background: rgba(255,255,255,0.12); color: #ccc; }
        #gbp-stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
            padding: 8px 10px;
            border-bottom: 0.5px solid rgba(255,255,255,0.06);
        }
        .gbp-stat {
            border-radius: 6px;
            padding: 5px 8px;
        }
        .gbp-stat-label { font-size: 9px; letter-spacing: 0.08em; margin-bottom: 1px; font-weight: 600; }
        .gbp-stat-value { font-size: 11px; color: #e0e0e0; font-weight: 500; }
        .gbp-stat.server { background: rgba(29,233,182,0.08); border: 0.5px solid rgba(29,233,182,0.2); }
        .gbp-stat.server .gbp-stat-label { color: #1de9b6; }
        .gbp-stat.autoplay { background: rgba(255,183,0,0.08); border: 0.5px solid rgba(255,183,0,0.2); }
        .gbp-stat.autoplay .gbp-stat-label { color: #ffb700; }
        #gbp-log {
            flex: 1;
            overflow-y: auto;
            padding: 6px 10px;
            display: flex;
            flex-direction: column;
            gap: 2px;
            max-height: 240px;
            min-height: 80px;
        }
        #gbp-log::-webkit-scrollbar { width: 3px; }
        #gbp-log::-webkit-scrollbar-track { background: transparent; }
        #gbp-log::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
        .gbp-entry { display: flex; gap: 7px; align-items: baseline; line-height: 1.5; }
        .gbp-ts { font-size: 10px; color: #444; flex-shrink: 0; }
        .gbp-msg { font-size: 11px; }
        .gbp-msg.info    { color: #90a4ae; }
        .gbp-msg.success { color: #1de9b6; }
        .gbp-msg.incoming{ color: #64b5f6; }
        .gbp-msg.outgoing{ color: #ce93d8; }
        .gbp-msg.active  { color: #ffb700; }
        .gbp-msg.command { color: #f48fb1; }
        .gbp-msg.error   { color: #ef5350; }
        #gbp-footer {
            padding: 5px 10px;
            border-top: 0.5px solid rgba(255,255,255,0.06);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        #gbp-count { font-size: 10px; color: #444; }
        #gbp-clear { background: none; border: none; color: #444; font-size: 10px; cursor: pointer; font-family: inherit; padding: 0; }
        #gbp-clear:hover { color: #888; }
        #gbp-pause-btn {
            position: fixed;
            bottom: 20px;
            right: 350px;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: #16213e;
            border: 0.5px solid rgba(255,255,255,0.15);
            color: #e0e0e0;
            font-size: 16px;
            cursor: pointer;
            z-index: 999998;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4);
            display: none;
            align-items: center;
            justify-content: center;
            user-select: none;
            transition: background 0.2s;
        }
        #gbp-pause-btn:hover { background: #1a2a52; }
        #gbp-pause-btn.paused { background: #4a3000; border-color: rgba(255,183,0,0.4); }
    `;

    const styleEl = document.createElement('style');
    styleEl.textContent = styles;
    document.head.appendChild(styleEl);

    const panel = document.createElement('div');
    panel.id = 'gbp-panel';
    panel.innerHTML = `
        <div id="gbp-header">
            <div id="gbp-header-left">
                <div id="gbp-dot"></div>
                <span id="gbp-title">69SHUBA BRIDGE</span>
            </div>
            <div id="gbp-header-right">
                <button class="gbp-btn" id="gbp-min-btn">_</button>
                <button class="gbp-btn" id="gbp-close-btn">×</button>
            </div>
        </div>
        <div id="gbp-stats">
            <div class="gbp-stat server">
                <div class="gbp-stat-label">SERVER</div>
                <div class="gbp-stat-value" id="gbp-server-status">Connecting...</div>
            </div>
            <div class="gbp-stat autoplay">
                <div class="gbp-stat-label">AUTOPLAY</div>
                <div class="gbp-stat-value" id="gbp-autoplay-status">Active</div>
            </div>
        </div>
        <div id="gbp-log"></div>
        <div id="gbp-footer">
            <span id="gbp-count">0 events</span>
            <button id="gbp-clear">clear</button>
        </div>
    `;
    document.body.appendChild(panel);

    const pauseBtn = document.createElement('div');
    pauseBtn.id = 'gbp-pause-btn';
    pauseBtn.title = 'Pause/Resume autoplay (Alt+P)';
    pauseBtn.textContent = '⏸';
    document.body.appendChild(pauseBtn);

    // --- Panel controls ---
    let minimized = false;
    let eventCount = 0;

    document.getElementById('gbp-min-btn').onclick = () => {
        minimized = !minimized;
        panel.classList.toggle('minimized', minimized);
        document.getElementById('gbp-min-btn').textContent = minimized ? '□' : '_';
    };
    document.getElementById('gbp-close-btn').onclick = () => {
        panel.style.display = 'none';
    };
    document.getElementById('gbp-clear').onclick = () => {
        document.getElementById('gbp-log').innerHTML = '';
        eventCount = 0;
        document.getElementById('gbp-count').textContent = '0 events';
    };

    // --- Dragging ---
    (function makeDraggable() {
        const header = document.getElementById('gbp-header');
        let isDragging = false, startX, startY, startRight, startBottom;
        header.addEventListener('mousedown', e => {
            if (e.target.classList.contains('gbp-btn')) return;
            isDragging = true;
            startX = e.clientX; startY = e.clientY;
            const rect = panel.getBoundingClientRect();
            startRight = window.innerWidth - rect.right;
            startBottom = window.innerHeight - rect.bottom;
        });
        document.addEventListener('mousemove', e => {
            if (!isDragging) return;
            const dx = e.clientX - startX, dy = e.clientY - startY;
            panel.style.right = Math.max(0, startRight - dx) + 'px';
            panel.style.bottom = Math.max(0, startBottom - dy) + 'px';
            panel.style.left = 'auto'; panel.style.top = 'auto';
        });
        document.addEventListener('mouseup', () => { isDragging = false; });
    })();

    // ==========================================
    // 📝 LOGGING HELPERS
    // ==========================================
    function now() {
        return new Date().toLocaleTimeString('en-GB', { hour12: false });
    }

    function log(message, type = 'info') {
        if (minimized) panel.classList.remove('minimized') && (minimized = false);
        const logEl = document.getElementById('gbp-log');
        const entry = document.createElement('div');
        entry.className = 'gbp-entry';
        entry.innerHTML = `<span class="gbp-ts">${now()}</span><span class="gbp-msg ${type}">${message}</span>`;
        logEl.appendChild(entry);
        logEl.scrollTop = logEl.scrollHeight;
        eventCount++;
        document.getElementById('gbp-count').textContent = `${eventCount} event${eventCount !== 1 ? 's' : ''}`;
    }

    function setDot(state) {
        const dot = document.getElementById('gbp-dot');
        dot.className = '';
        if (state === 'ok') dot.classList.add('ok');
        if (state === 'warn') dot.classList.add('warn');
        if (state === 'err') dot.classList.add('err');
    }

    function setServerStatus(text) {
        document.getElementById('gbp-server-status').textContent = text;
    }

    function setAutoplayStatus(text) {
        document.getElementById('gbp-autoplay-status').textContent = text;
    }
    // ==========================================
    // ⌨️ CONTROLS
    // ==========================================
    function togglePause() {
        isPaused = !isPaused;
        if (isPaused) {
            log("⏸️ AUTOPLAY PAUSED (Alt+P or button to resume)", "command");
            setAutoplayStatus("Paused");
            setDot("warn");
            pauseBtn.textContent = '▶';
            pauseBtn.classList.add('paused');
            pauseBtn.title = 'Resume autoplay (Alt+P)';
        } else {
            log("▶️ AUTOPLAY RESUMED", "command");
            setAutoplayStatus("Active");
            setDot("ok");
            log("You might need to press the Right Arrow key manually if you missed an automated turn.", "info");
            pauseBtn.textContent = '⏸';
            pauseBtn.classList.remove('paused');
            pauseBtn.title = 'Pause autoplay (Alt+P)';
        }
    }

    pauseBtn.onclick = togglePause;

    window.addEventListener('keydown', (e) => {
        if (e.altKey && e.key.toLowerCase() === 'p') {
            togglePause();
        }
    });

    window.addEventListener('load', function () {
        setDot('warn');
        setAutoplayStatus(isPaused ? "Paused" : "Active");
        // Give the page a moment to fully render
        setTimeout(scrapeAndSend, 500);
    });

    function scrapeAndSend() {
        // 69shuba puts the main story text inside a div with class 'txtnav'
        const contentArea = document.querySelector('.txtnav');
        if (!contentArea) {
            log("Content div '.txtnav' not found.", "error");
            return;
        }

        const novelText = contentArea.innerText.trim();
        if (!novelText) {
            log("No text found in content area.", "error");
            return;
        }

        log(`Sending chapter content (${novelText.length} chars)...`, "outgoing");
        const isActive = document.visibilityState === 'visible';
        GM_xmlhttpRequest({
            method: "POST",
            url: `${SERVER_URL}/book-content`,
            headers: { "Content-Type": "application/json" },
            data: JSON.stringify({
                content: novelText,
                isActive: isActive
            }),
            onload: function (response) {
                panel.style.display = 'flex';
                pauseBtn.style.display = 'flex';
                setServerStatus('Connected');
                if (!isPaused) setDot('ok');
                
                try {
                    const data = JSON.parse(response.responseText);
                    handleServerResponse(data);
                } catch (e) {
                    log("Failed to parse server response.", "error");
                }
            },
            onerror: function (error) {
                panel.style.display = 'none';
                pauseBtn.style.display = 'none';
                setServerStatus('Offline');
                setDot('err');
                log("Could not reach translation server. Is it running?", "error");
            }
        });
    }

    function handleServerResponse(data) {
        if (data.action === "next") {
            log("Server says go to next page.", "success");
            if (isPaused) {
                log("⏸️ Paused via hotkey. Will not go to next page automatically.", "warn");
                setTimeout(pollStatus, 5000);
            } else {
                goToNextPage();
            }
        } else if (data.action === "wait") {
            const retrySeconds = data.retryAfter || 5;
            log(`Server says wait. Polling in ${retrySeconds}s...`, "active");
            setTimeout(pollStatus, retrySeconds * 1000);
        } else {
            log(`Unknown action from server: ${JSON.stringify(data)}`, "warn");
        }
    }
    function pollStatus() {
        log("Polling /book-status...", "info");
        const isActive = document.visibilityState === 'visible';

        GM_xmlhttpRequest({
            method: "GET",
            url: `${SERVER_URL}/book-status?isActive=${isActive}`,
            headers: { "Content-Type": "application/json" },
            onload: function (response) {
                panel.style.display = 'flex';
                pauseBtn.style.display = 'flex';
                setServerStatus('Connected');
                if (!isPaused) setDot('ok');
                
                try {
                    const data = JSON.parse(response.responseText);
                    handleServerResponse(data);
                } catch (e) {
                    log("Failed to parse status response.", "error");
                }
            },
            onerror: function (error) {
                panel.style.display = 'none';
                pauseBtn.style.display = 'none';
                setServerStatus('Offline');
                setDot('err');
                log("Status poll failed, retrying in 5s...", "error");
                setTimeout(pollStatus, 5000);
            }
        });
    }

    function goToNextPage() {
        log("Simulating right arrow key press to navigate to next chapter...", "command");
        const event = new KeyboardEvent('keydown', {
            key: 'ArrowRight',
            code: 'ArrowRight',
            keyCode: 39,
            which: 39,
            bubbles: true,
            cancelable: true
        });
        document.dispatchEvent(event);
    }

})();
