// ==UserScript==
// @name         Ticarisk Bot — Captcha Solver + TRT Auto Cycle v3.4
// @namespace    http://tampermonkey.net/
// @version      3.4
// @description  Solves the slider captcha on every ticarisk page, self-updates, and runs the production/farm cycle on Europe/Istanbul time while the PC is on
// @author       Assistant
// @match        https://www.ticarisk.com/*
// @match        https://ticarisk.com/*
// @require      https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js
// @run-at       document-start
// @grant        none
// @downloadURL  https://raw.githubusercontent.com/OWNER/REPO/main/ticarisk-math-bot.user.js
// @updateURL    https://raw.githubusercontent.com/OWNER/REPO/main/ticarisk-math-bot.user.js
// ==/UserScript==

(function () {
    'use strict';

    // ---- self-update -----------------------------------------------------
    // Point both URLs above (and this one) at the raw copy you keep in a repo.
    // Leave empty to disable the runtime check entirely.
    const UPDATE_URL = '';
    const VERSION = '3.4';

    // ---- game cycle on Turkish time --------------------------------------
    const SCHEDULE_MINUTE = 23;              // run at :23 every hour, Europe/Istanbul
    const CYCLE_SECTIONS = ['production', 'fields', 'orchards', 'barns', 'coops', 'bees'];
    const CROP_ID = 4;                       // 4 = Potato (6h), 1 = Wheat (4h)
    const CROP_ROTATION = true;              // walk the crop list forward each planting
    const CROP_KEY = 'ticarisk_crop_idx';
    const FEED_ANIMALS = true;
    const PETEK_REFILL = true;               // buy honeycomb back after a harvest
    const PETEK_PRICE = 1500;                // $ per honeycomb
    const PETEK_MAX_SPEND = 60000;           // hard ceiling per refill
    const MATH_GRIND = true;                 // solve every available question each cycle
    const MATH_WITHDRAW = true;              // move the math vault into the balance
    const MATH_MAX_ROUNDS = 600;             // safety stop
    const BUY_WATER_LITERS = 0;              // auto-buy water when planting needs it (0 = off)
    const MAX_WATER_SPEND = 0;               // $ ceiling on those buys (0 = balance only)

    const MIN_ANSWER_MS = 1400;   // server rejects answers faster than ~1s
    const T_EDGES = [45, 35, 26, 20];
    const MIN_RUN = 30;
    const MAX_ROUNDS = 4;         // failed verifies total — server locks at 5
    const VERIFY_TIMEOUT = 5000;

    let worker = null;
    let ocrBusy = false;
    let queuedQuestion = null;
    let autoSubmit = false;
    let mathBusy = false;   // math grind running — the page auto-solver must stay out

    let questionAt = 0;
    let lastPuzzleData = null;
    let puzzleAt = 0;             // when the captured puzzle arrived
    let puzzleSeq = 0;            // bumped on every fresh puzzle_data
    let lastVerify = null;        // {ok, locked, msg, seq}
    let captchaRunning = false;
    let captchaRounds = 0;
    let modalWatcher = null;

    /* ============================== UI ============================== */

    function el(id) { return document.getElementById(id); }

    function setStatus(t) {
        const n = el('math-solver-status'); if (n) n.textContent = t;
    }
    function setEq(t) {
        const n = el('math-detected-eq'); if (n) n.textContent = t;
    }
    function setAns(t) {
        const n = el('math-calc-ans'); if (n) n.textContent = t;
    }
    function setCaptcha(t) {
        const n = el('math-captcha-status'); if (n) n.textContent = t;
    }

    function createUI() {
        if (el('math-solver-panel')) return;
        const box = document.createElement('div');
        box.id = 'math-solver-panel';
        box.style.cssText = `
            position: fixed; top: 20px; right: 20px;
            background: #0f172a; color: #f8fafc;
            border: 2px solid #10b981; border-radius: 12px;
            padding: 14px 18px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 14px; z-index: 2147483647;
            box-shadow: 0 8px 24px rgba(0,0,0,0.6); min-width: 230px;
        `;
        box.innerHTML = `
            <div style="font-weight:bold; color:#10b981; font-size:15px; margin-bottom:6px;">🧮 Ticarisk Bot v3.4</div>
            <div style="font-size:12px; color:#94a3b8;">Detected Question:</div>
            <div id="math-detected-eq" style="font-size:16px; font-weight:bold; color:#facc15; margin:4px 0;">Waiting for round...</div>
            <div style="font-size:12px; color:#94a3b8;">Calculated Answer:</div>
            <div id="math-calc-ans" style="font-size:24px; font-weight:800; color:#34d399; margin:4px 0;">---</div>
            <div id="math-solver-status" style="font-size:11px; color:#64748b; margin-bottom:4px;">Initializing OCR...</div>
            <div style="font-size:12px; color:#94a3b8; margin-top:6px;">Captcha:</div>
            <div id="math-captcha-status" style="font-size:11px; color:#818cf8; margin-bottom:8px;">idle</div>
            <div style="display:flex; gap:6px; margin-bottom:6px;">
                <button id="math-solve-btn" style="flex:1; padding:6px; background:#2563eb; color:#fff; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Scan Screen Now</button>
            </div>
            <label style="display:flex; align-items:center; gap:6px; font-size:12px; color:#94a3b8; cursor:pointer;">
                <input type="checkbox" id="math-auto-submit" checked> Auto-Submit Answers
            </label>
            <div style="border-top:1px solid #1e293b; margin-top:8px; padding-top:8px;">
                <div style="font-size:12px; color:#94a3b8;">Game cycle — Europe/Istanbul</div>
                <div id="cycle-clock" style="font-size:11px; color:#64748b;">TRT --:--</div>
                <div id="cycle-status" style="font-size:11px; color:#facc15; margin:3px 0; word-break:break-word;">idle</div>
                <label style="display:flex; align-items:center; gap:6px; font-size:12px; color:#94a3b8; cursor:pointer;">
                    <input type="checkbox" id="cycle-enable"> Auto-run at :23 TRT (every hour)
                </label>
                <div style="display:flex; gap:6px; margin-top:6px;">
                    <button id="cycle-now" style="flex:1; padding:6px; background:#7c3aed; color:#fff; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Run cycle now</button>
                    <button id="upd-now" style="flex:1; padding:6px; background:#0ea5e9; color:#fff; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Check update</button>
                </div>
                <div style="border-top:1px solid #1e293b; margin-top:8px; padding-top:8px;">
                    <div style="font-size:12px; color:#94a3b8;">Math grind — every hour with the cycle</div>
                    <div id="math-grind-status" style="font-size:11px; color:#facc15; margin:3px 0; word-break:break-word;">idle</div>
                    <div style="display:flex; gap:6px; margin-top:6px;">
                        <button id="math-grind-now" style="flex:1; padding:6px; background:#f59e0b; color:#111; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Solve all math now</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(box);

        el('math-solve-btn').onclick = () => manualScan();
        el('math-auto-submit').checked = autoSubmit;
        el('math-auto-submit').onchange = (e) => { autoSubmit = e.target.checked; };
        const ce = el('cycle-enable');
        ce.checked = !!(cfg.cycleOn && cfg.scheduleOn);
        ce.onchange = (e) => {
            cfg.cycleOn = e.target.checked;
            cfg.scheduleOn = e.target.checked;
            saveCfg();
            setCycle(e.target.checked ? 'scheduled — ' + SCHEDULE_MINUTE + ':00 past each hour (TRT)' : 'paused');
        };
        el('cycle-now').onclick = () => runCycle();
        el('math-grind-now').onclick = () => mathGrind('manual');
        el('upd-now').onclick = () => checkForUpdate();
        setCycle(cfg.cycleOn ? 'scheduled — :' + String(SCHEDULE_MINUTE).padStart(2, '0') + ' past each hour (TRT)' : 'paused');
    }

    /* ============================== OCR ============================== */

    async function initOCR() {
        setStatus('Loading OCR engine...');
        try {
            worker = await Tesseract.createWorker('eng');
            await worker.setParameters({
                tessedit_char_whitelist: '0123456789+-*x '
            });
            setStatus('OCR ready');
        } catch (e) {
            console.error('[MathBot] OCR init error', e);
            setStatus('OCR error: ' + e.message);
        }
    }

    function cleanImage(src) {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        const scale = 2;
        const w = (src.naturalWidth || src.width || 300) * scale;
        const h = (src.naturalHeight || src.height || 80) * scale;
        canvas.width = w; canvas.height = h;
        ctx.drawImage(src, 0, 0, w, h);

        const imgData = ctx.getImageData(0, 0, w, h);
        const d = imgData.data;
        const binary = new Uint8Array(w * h);
        for (let i = 0; i < d.length; i += 4) {
            binary[i / 4] = ((d[i] + d[i + 1] + d[i + 2]) / 3) > 95 ? 1 : 0;
        }
        for (let y = 1; y < h - 1; y++) {
            for (let x = 1; x < w - 1; x++) {
                const idx = y * w + x;
                if (binary[idx] !== 1) continue;
                let n = 0;
                for (let dy = -1; dy <= 1; dy++) {
                    for (let dx = -1; dx <= 1; dx++) {
                        if (dx === 0 && dy === 0) continue;
                        if (binary[(y + dy) * w + (x + dx)] === 1) n++;
                    }
                }
                if (n < 2) binary[idx] = 0;
            }
        }
        for (let i = 0; i < binary.length; i++) {
            const v = binary[i] === 1 ? 0 : 255;
            d[i * 4] = v; d[i * 4 + 1] = v; d[i * 4 + 2] = v; d[i * 4 + 3] = 255;
        }
        ctx.putImageData(imgData, 0, 0);
        return canvas;
    }

    function loadImage(url) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            img.onload = () => resolve(img);
            img.onerror = () => reject(new Error('image load failed'));
            img.src = url;
        });
    }

    /* ====================== question handling ======================= */

    function queueQuestion(q) {
        if (!q || !q.question_image_url) return;
        if (mathBusy) return;   // the grind owns this question, don't double-submit
        questionAt = Date.now();
        setEq('scanning...');
        if (ocrBusy) { queuedQuestion = q; return; }
        processQuestion(q);
    }

    async function processQuestion(q) {
        ocrBusy = true;
        let cur = q;
        try {
            while (cur) {
                queuedQuestion = null;
                await solveOne(cur);
                cur = queuedQuestion;
            }
        } finally {
            ocrBusy = false;
        }
    }

    async function readQuestion(url, islemTipi) {
        if (!worker) {
            setStatus('OCR not ready, retrying...');
            try { await initOCR(); } catch (e) { console.error('[MathBot] lazy OCR init', e); }
            for (let i = 0; i < 40 && !worker; i++) await sleep(250);
            if (!worker) { setStatus('OCR unavailable'); return null; }
        }
        const img = await loadImage(url);
        const clean = cleanImage(img);
        const ret = await worker.recognize(clean);
        const raw = (ret.data.text || '').trim();

        setEq(raw.replace(/\s+/g, ' ') || 'Scanning...');

        const nums = (raw.match(/\d+/g) || []).map(Number);
        if (nums.length < 2) {
            setStatus('Could not parse — use Scan Screen Now');
            return null;
        }
        const a = nums[0], b = nums[1];
        const op = pickOp(islemTipi, raw);
        const ans = op === '-' ? a - b : (op === '*' ? a * b : a + b);

        setAns(String(ans));
        setStatus(`${a} ${op} ${b} = ${ans}`);
        return { a: a, b: b, op: op, ans: ans, raw: raw };
    }

    async function solveOne(q) {
        try {
            const r = await readQuestion(q.question_image_url, q.islem_tipi);
            if (!r) return;
            submitAnswer(r.ans);
        } catch (e) {
            console.error('[MathBot] solve error', e);
            setStatus('Error: ' + e.message);
        }
    }

    // server-provided islem_tipi wins; OCR text is only a fallback
    function pickOp(islemTipi, text) {
        const map = { toplama: '+', cikarma: '-', carpma: '*' };
        if (islemTipi && map[islemTipi]) return map[islemTipi];
        if (text.indexOf('+') >= 0) return '+';
        if (text.indexOf('-') >= 0) return '-';
        if (text.indexOf('*') >= 0 || text.indexOf('x') >= 0 || text.indexOf('X') >= 0) return '*';
        return '+';
    }

    /* ========================= auto submit ========================== */

    function submitAnswer(ans) {
        // exact ids used by matematik.php
        const input = el('answerInput') ||
            document.querySelector('input.answer-input') ||
            document.querySelector('input[name="cevap"]');
        const btn = el('submitBtn');

        if (!input) { setStatus('answerInput not found'); return; }

        input.value = String(ans);
        input.dispatchEvent(new Event('input', { bubbles: true }));   // fires toggleSubmitButton()
        input.dispatchEvent(new Event('change', { bubbles: true }));

        if (!autoSubmit) { setStatus('filled (auto-submit off) — press Submit'); return; }
        if (!btn) { setStatus('submitBtn not found'); return; }

        const wait = Math.max(0, MIN_ANSWER_MS - (Date.now() - questionAt));
        setTimeout(() => {
            if (document.contains(btn) === false) { setStatus('submit button gone'); return; }
            if (btn.disabled) { setStatus('submit still disabled'); return; }
            btn.click();
            setStatus('submitted');
        }, wait);
    }

    function manualScan() {
        const img = Array.from(document.querySelectorAll('img')).find(i =>
            !/logo|avatar|icon/i.test(i.src));
        const canvas = document.getElementById('questionCanvas');
        if (img && img.complete) {
            questionAt = Date.now() - MIN_ANSWER_MS;
            solveOne({ question_image_url: img.src, islem_tipi: null });
        } else if (canvas) {
            questionAt = Date.now() - MIN_ANSWER_MS;
            solveOne({ question_image_url: canvas.toDataURL(), islem_tipi: null });
        } else {
            setStatus('no question image found');
        }
    }

    /* ======================= network intercept ====================== */

    function parseResponse(text, body) {
        let data;
        try { data = JSON.parse(text); } catch (e) { return; }
        if (!data || typeof data !== 'object') return;

        const req = typeof body === 'string' ? body : (body ? String(body) : '');

        if (req.indexOf('action=verify_human') >= 0 || req.indexOf('"verify_human"') >= 0) {
            lastVerify = {
                ok: data.success === true || data.success === 1 || data.success === 'true',
                locked: data.locked === true || data.locked === 1 || data.locked === 'true',
                msg: data.message || '',
                at: Date.now()
            };
            if (lastVerify.ok) setCaptcha('server: position accepted');
            else setCaptcha('server: ' + (lastVerify.msg || 'rejected') +
                (captchaRounds < MAX_ROUNDS ? ' — retrying' : ' — giving up'));
            if (lastVerify.ok) watchModal();
            return;
        }

        if (data.puzzle_data && data.puzzle_data.challenge_token) {
            lastPuzzleData = data.puzzle_data;
            puzzleAt = Date.now();
            puzzleSeq++;
        }

        if (data.verification_required) {
            setStatus('security verification required');
            setCaptcha('captcha detected — cracker armed');
            captchaRounds = 0;
            lastVerify = null;
            watchModal();
            return;
        }

        if (typeof data.dogru === 'boolean') {
            if (data.dogru) setStatus('correct  +$' + data.odul);
            else setStatus('wrong — correct: ' + data.dogru_cevap);
        }

        if (data.question_image_url) queueQuestion(data);
        else if (data.next_question && data.next_question.question_image_url) queueQuestion(data.next_question);
    }

    const origFetch = window.fetch;
    window.fetch = async function (...args) {
        const res = await origFetch.apply(this, args);
        let bodyText = '';
        try {
            const b = args[1] && args[1].body;
            if (typeof b === 'string') bodyText = b;
            else if (b && typeof b.toString === 'function') bodyText = b.toString();
            else if (args[1] && args[1].method === 'POST') {
                bodyText = new URLSearchParams(args[1].body || '').toString();
            }
        } catch (e) {}
        try { res.clone().text().then(t => parseResponse(t, bodyText)).catch(() => {}); } catch (e) {}
        return res;
    };

    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, url) {
        this.__mb_url = url;
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
        this.__mb_body = (typeof body === 'string') ? body
            : (body && typeof body.toString === 'function') ? body.toString() : '';
        this.addEventListener('load', function () {
            // parse every response: on businesses.php (and the farm pages) the
            // puzzle posts to *the current page*, not to matematik.php.
            try { parseResponse(this.responseText, this.__mb_body); } catch (e) {}
        });
        return origSend.apply(this, arguments);
    };

    /* ====================== captcha: detection ====================== */

    function runs(get, n, minlen) {
        const out = [];
        let s = -1;
        for (let i = 0; i <= n; i++) {
            const v = i < n ? !!get(i) : false;
            if (v) { if (s < 0) s = i; }
            else {
                if (s >= 0 && (i - s) >= minlen) out.push([s, i - 1, i - s]);
                s = -1;
            }
        }
        return out;
    }

    /* --------- NCC: match the cut piece against every slot position -------- */

    function nccDetect(bg, piece, pw, ph, py, topk, gap) {
        if (!piece || py === null || py === undefined) return [];
        topk = topk || 5; gap = gap || 6;
        const w = bg.width, h = bg.height, d = bg.data, td = piece.data;
        const W = Math.min(pw, piece.width), H = Math.min(ph, piece.height);
        const y0 = Math.round(py);
        if (W < 8 || H < 8 || y0 < 0 || y0 + H > h) return [];
        const N = W * H, M = 3 * N;

        let ts = 0, tsq = 0;
        for (let y = 0; y < H; y++) {
            for (let x = 0; x < W; x++) {
                const i = (y * piece.width + x) * 4;
                const a = td[i], b = td[i + 1], c = td[i + 2];
                ts += a + b + c; tsq += a * a + b * b + c * c;
            }
        }
        const tm = ts / M;
        const tVar = tsq - M * tm * tm;
        if (tVar <= 1e-6) return [];

        const scores = new Float32Array(w - W + 1);
        for (let x = 0; x <= w - W; x++) {
            let s = 0, sq = 0, dot = 0;
            for (let y = 0; y < H; y++) {
                const brow = ((y0 + y) * w + x) * 4;
                const trow = (y * piece.width) * 4;
                for (let xx = 0; xx < W; xx++) {
                    const bi = brow + (xx << 2), ti = trow + (xx << 2);
                    const a = d[bi], b = d[bi + 1], c = d[bi + 2];
                    s += a + b + c;
                    sq += a * a + b * b + c * c;
                    dot += td[ti] * a + td[ti + 1] * b + td[ti + 2] * c;
                }
            }
            const sm = s / M;
            const sVar = sq - M * sm * sm;
            const cov = dot - M * tm * sm;
            const den = Math.sqrt(tVar * sVar);
            scores[x] = den > 1e-6 ? cov / den : -1;
        }

        const order = [];
        for (let x = 0; x < scores.length; x++) order.push(x);
        order.sort((a, b) => scores[b] - scores[a]);

        const maxPct = ((w - pw) / w) * 100;
        const out = [], picked = [];
        for (const x of order) {
            let near = false;
            for (const p of picked) if (Math.abs(x - p) < gap) { near = true; break; }
            if (near) continue;
            picked.push(x);
            out.push({ pct: Math.max(0, Math.min(x / w * 100, maxPct)), conf: scores[x] });
            if (out.length >= topk) break;
        }
        return out;
    }

    /* --- merge detectors: NCC only leads when its peak is clearly dominant -- */

    function buildCandidates(score, ncc, edges) {
        const out = [], seen = {};
        const add = (pct, tag) => {
            if (pct === null || pct === undefined || isNaN(pct)) return;
            const k = pct.toFixed(2);
            if (seen[k]) return;
            seen[k] = 1;
            out.push({ pct, tag });
        };
        const n0 = ncc[0], n1 = ncc[1];
        const dominant = !!n0 && n0.conf >= 0.55 && (!n1 || (n0.conf - n1.conf) >= 0.12);
        if (dominant) { add(n0.pct, 'piece'); add(score[0] && score[0].pct, 'score'); }
        else { add(score[0] && score[0].pct, 'score'); add(n0 && n0.pct, 'piece'); }
        for (let i = 1; i < score.length; i++) add(score[i].pct, 'score');
        for (let i = 1; i < ncc.length; i++) add(ncc[i].pct, 'piece');
        for (const c of (edges || [])) add(c.pct, 'edges');
        return out;
    }

    /* ----- 4-side exhaustive scorer (beats run-pairing on coloured slots) --- */

    function scoreDetect(imgData, pw, ph, py, margin, topk) {
        margin = margin || 4;
        topk = topk || 6;
        const w = imgData.width, h = imgData.height, d = imgData.data;
        if (py === null || py === undefined) py = Math.round(h * 0.4);

        const diff3 = (i, a, b) => (
            Math.abs(d[i] - 0.5 * (d[a] + d[b])) +
            Math.abs(d[i + 1] - 0.5 * (d[a + 1] + d[b + 1])) +
            Math.abs(d[i + 2] - 0.5 * (d[a + 2] + d[b + 2]))) / 3;

        // vertical-line strength per column, averaged over the slot's row band
        const y0 = Math.max(0, py), y1 = Math.min(h, py + ph);
        const rows = Math.max(1, y1 - y0);
        const cv = new Float32Array(w);
        for (let y = y0; y < y1; y++) {
            for (let x = 3; x < w - 3; x++) {
                const i = (y * w + x) * 4;
                cv[x] += diff3(i, i - 12, i + 12);
            }
        }
        for (let x = 0; x < w; x++) cv[x] /= rows;

        // horizontal-line strength per column, on the top and bottom edge rows
        const hprof = (yy) => {
            const prof = new Float32Array(w);
            const n = Math.min(h - 4, Math.max(3, yy + 1)) - Math.max(0, yy - 1) + 1;
            const cnt = Math.max(1, n);
            for (let k = -1; k <= 1; k++) {
                const y = yy + k;
                if (y < 3 || y > h - 4) continue;
                for (let x = 3; x < w - 3; x++) {
                    const i = (y * w + x) * 4;
                    prof[x] += diff3(i, i - 12 * w, i + 12 * w);
                }
            }
            for (let x = 0; x < w; x++) prof[x] /= cnt;
            return prof;
        };
        const tp = hprof(py), bp = hprof(py + ph);

        const win = Math.max(4, pw - 2 * margin);
        const loMax = w - margin - pw;
        const raw = [];
        for (let L = margin; L < loMax; L++) {
            let t = 0, b = 0;
            for (let x = L; x < L + win; x++) { t += tp[x]; b += bp[x]; }
            raw.push({ L, score: cv[L] + cv[L + pw] + t / win + b / win });
        }
        if (!raw.length) return [];
        raw.sort((a, b) => b.score - a.score);

        // a painted slot uses one colour on all four sides
        const mean = (ys, ye, xs, xe) => {
            let r = 0, g = 0, bl = 0, n = 0;
            for (let y = ys; y < ye; y++) for (let x = xs; x < xe; x++) {
                const i = (y * w + x) * 4; r += d[i]; g += d[i + 1]; bl += d[i + 2]; n++;
            }
            return n ? [r / n, g / n, bl / n] : [0, 0, 0];
        };
        for (let k = 0; k < raw.length && k < 72; k++) {
            const L = raw[k].L;
            if (L - 1 < 0 || L + pw + 2 > w) continue;
            const cols = [
                mean(y0, y1, L - 1, L + 2),
                mean(y0, y1, L + pw - 1, L + pw + 2),
                mean(py, Math.min(h, py + 3), L + margin, L + pw - margin),
                mean(Math.max(0, py + ph - 3), Math.min(h, py + ph), L + margin, L + pw - margin)
            ];
            const mu = [0, 1, 2].map(c => (cols[0][c] + cols[1][c] + cols[2][c] + cols[3][c]) / 4);
            let spread = 0;
            for (let c = 0; c < 3; c++) {
                let s = 0;
                for (let q = 0; q < 4; q++) s += Math.pow(cols[q][c] - mu[c], 2);
                spread += Math.sqrt(s / 4);
            }
            spread /= 3;
            raw[k].total = raw[k].score + 1.6 * Math.max(0, 70 - spread);
        }
        for (const r of raw) if (r.total === undefined) r.total = r.score;

        raw.sort((a, b) => b.total - a.total);
        const maxPct = ((w - pw) / w) * 100;
        const out = [], seen = {};
        for (const r of raw) {
            const pct = Math.max(0, Math.min(r.L / w * 100, maxPct));
            const key = pct.toFixed(2);
            if (seen[key]) continue;
            seen[key] = 1;
            out.push({ pct, lo: r.L, hi: r.L + pw, span: pw, method: 'score', T: 0 });
            if (out.length >= topk) break;
        }
        return out;
    }

    // Port of solve_captcha2.find_candidates — edge-run rectangle finder.
    function detectCandidates(imgData, pw, ph, py) {
        const w = imgData.width, h = imgData.height, d = imgData.data;
        const Eh = new Float32Array((h - 3) * w);
        const Ev = new Float32Array(h * (w - 3));

        for (let y = 0; y < h - 3; y++) {
            for (let x = 0; x < w; x++) {
                const i = (y * w + x) * 4, j = ((y + 3) * w + x) * 4;
                Eh[y * w + x] = (Math.abs(d[i] - d[j]) + Math.abs(d[i + 1] - d[j + 1]) +
                    Math.abs(d[i + 2] - d[j + 2])) / 3;
            }
        }
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w - 3; x++) {
                const i = (y * w + x) * 4, j = (y * w + x + 3) * 4;
                Ev[y * (w - 3) + x] = (Math.abs(d[i] - d[j]) + Math.abs(d[i + 1] - d[j + 1]) +
                    Math.abs(d[i + 2] - d[j + 2])) / 3;
            }
        }

        const bandLo = (py === null || py === undefined) ? 0 : Math.max(0, py - 25);
        const bandHi = (ph === null || ph === undefined) ? (h - 3)
            : Math.min(h - 3, py + ph + 25);
        const minV = ph ? ph * 0.55 : MIN_RUN;
        const minH = pw * 0.55;

        const out = [];
        const seen = {};

        for (const T of T_EDGES) {
            const vedges = [];
            for (let x = 0; x < w - 3; x++) {
                const col = x;
                const rs = runs(i => Ev[(bandLo + i) * (w - 3) + col] > T,
                    bandHi - bandLo, Math.max(MIN_RUN, minV));
                for (const [y0, y1, L] of rs) {
                    vedges.push({ x, y0: y0 + bandLo, y1: y1 + bandLo, L });
                }
            }
            const hedges = [];
            for (let y = bandLo; y < bandHi; y++) {
                const rs = runs(i => Eh[y * w + i] > T, w, Math.max(MIN_RUN, minH));
                for (const [x0, x1, L] of rs) {
                    hedges.push({ y, x0, x1, L });
                }
            }

            // method A: two horizontal edges spanning the box width
            for (let i = 0; i < hedges.length; i++) {
                for (let j = i + 1; j < hedges.length; j++) {
                    const a = hedges[i], b = hedges[j];
                    const lo = Math.min(a.x0, b.x0), hi = Math.max(a.x1, b.x1);
                    const span = hi - lo;
                    const centerOff = Math.abs((a.x0 + a.x1) - (b.x0 + b.x1));
                    if (span >= pw - 8 && span <= pw + 12 && centerOff <= Math.max(8, pw * 0.3)) {
                        const key = lo + ':' + hi;
                        if (seen[key]) continue;
                        seen[key] = 1;
                        out.push({ lo, hi, span, method: 'horizontal/T' + T, T });
                    }
                }
            }
            // method B: vertical edge pair
            for (let i = 0; i < vedges.length; i++) {
                for (let j = i + 1; j < vedges.length; j++) {
                    const a = vedges[i], b = vedges[j];
                    if (b.x <= a.x) continue;
                    const span = b.x - a.x;
                    if (span < pw - 8 || span > pw + 12) continue;
                    const key = a.x + ':' + b.x;
                    if (seen[key]) continue;
                    seen[key] = 1;
                    out.push({ lo: a.x, hi: b.x, span, method: 'vertical/T' + T, T });
                }
            }
        }

        out.sort((p, q) => {
            const dp = Math.abs(p.span - pw), dq = Math.abs(q.span - pw);
            return dp !== dq ? dp - dq : p.T - q.T;
        });

        const maxPct = ((w - pw) / w) * 100;
        for (const c of out) {
            const boxC = (c.lo + c.hi) / 2;
            const pieceLeft = boxC - pw / 2;
            let pct = (pieceLeft / w) * 100;
            c.pct = Math.max(0, Math.min(pct, maxPct));
        }

        // consensus: individual detections cluster around the true box; the median
        // of the tight cluster is a better first guess than any single threshold.
        if (out.length >= 2) {
            const base = out[0].pct;
            const cluster = out.map(c => c.pct)
                .filter(p => Math.abs(p - base) <= 2.0)
                .sort((a, b) => a - b);
            if (cluster.length >= 2) {
                const med = cluster[Math.floor(cluster.length / 2)];
                if (Math.abs(med - base) > 0.05) {
                    out.unshift({
                        lo: out[0].lo, hi: out[0].hi, span: out[0].span, pct: med,
                        method: 'consensus(n=' + cluster.length + ')', T: out[0].T
                    });
                }
            }
        }
        return out;
    }

    /* ======================= captcha: driving ======================= */

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    function watchModal() {
        if (modalWatcher) return;
        // permanent: every new puzzle creates a fresh #puzzleCanvas
        modalWatcher = setInterval(() => {
            const canvas = document.getElementById('puzzleCanvas');
            if (!canvas || captchaRunning) return;
            if (canvas.dataset.mbAttached) return;
            canvas.dataset.mbAttached = '1';
            runCaptcha(canvas);
        }, 200);
    }

    async function waitPainted(canvas) {
        for (let i = 0; i < 50; i++) {
            try {
                const ctx = canvas.getContext('2d');
                const w = Math.min(8, canvas.width || 0), h = Math.min(8, canvas.height || 0);
                if (w && h) {
                    const px = ctx.getImageData(0, 0, w, h).data;
                    for (let k = 3; k < px.length; k += 4) if (px[k] > 0) return true;
                }
            } catch (e) {}
            await sleep(120);
        }
        return false;
    }

    async function getBackground() {
        const pd = lastPuzzleData;
        if (!pd || !pd.background_image) return null;
        const token = (typeof puzzleChallengeToken !== 'undefined') ? puzzleChallengeToken : '';
        // the global token is authoritative for the puzzle that is on screen;
        // fall back to "captured recently" only when the global is still empty
        const fresh = token ? (pd.challenge_token === token) : (Date.now() - puzzleAt < 90000);
        if (!fresh) return null;
        try {
            const img = await loadImage(pd.background_image);
            const c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            const ctx = c.getContext('2d');
            ctx.drawImage(img, 0, 0);
            let piece = null;
            if (pd.piece_image) {
                try {
                    const pimg = await loadImage(pd.piece_image);
                    const pc = document.createElement('canvas');
                    pc.width = pimg.naturalWidth; pc.height = pimg.naturalHeight;
                    const pctx = pc.getContext('2d');
                    pctx.drawImage(pimg, 0, 0);
                    piece = pctx.getImageData(0, 0, pc.width, pc.height);
                } catch (e) { piece = null; }
            }
            return {
                data: ctx.getImageData(0, 0, c.width, c.height),
                piece: piece,
                pw: Number(pd.piece_width) || Math.floor(c.width * 0.125),
                ph: Number(pd.piece_height) || Math.floor(c.height * 0.2),
                py: (pd.piece_y === undefined || pd.piece_y === null) ? null : Number(pd.piece_y),
                src: 'network'
            };
        } catch (e) { return null; }
    }

    function dragTo(pct) {
        const track = document.querySelector('.puzzle-track');
        const btn = document.querySelector('.puzzle-slider-button');
        if (!track || !btn) return false;

        const tr = track.getBoundingClientRect();
        const btnSize = btn.offsetWidth || 40;
        const halfBtn = btnSize / 2;
        const maxDistance = Math.max(1, tr.width - btnSize);
        const cy = tr.top + tr.height / 2;

        const startX = tr.left + halfBtn;
        const nudgeX = startX + Math.min(8, maxDistance);
        const targetX = tr.left + halfBtn + (pct / 100) * maxDistance;

        const mk = (type, x, buttons) => new PointerEvent(type, {
            clientX: x, clientY: cy, pointerId: 1, isPrimary: true,
            pointerType: 'mouse', button: 0, buttons: buttons,
            bubbles: true, cancelable: true, composed: true
        });

        try {
            btn.dispatchEvent(mk('pointerdown', startX, 1));
            document.dispatchEvent(mk('pointermove', nudgeX, 1));
            document.dispatchEvent(mk('pointermove', targetX, 1));
            document.dispatchEvent(mk('pointerup', targetX, 0));
        } catch (e) {
            console.error('[MathBot] drag failed', e);
            return false;
        }
        return true;
    }

    function modalState() {
        const track = document.querySelector('.puzzle-track');
        if (!track) return 'gone';
        const status = document.querySelector('.puzzle-status');
        const txt = (status && status.textContent) || '';
        if (/failed attempts|try again in/i.test(txt)) return 'locked';
        return 'present';
    }

    // ground truth = the verify_human response, never the DOM:
    // a FAILED attempt also removes .puzzle-track (SweetAlert replaces the popup)
    async function waitForVerify(since, ms) {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
            if (lastVerify && lastVerify.at >= since) {
                if (lastVerify.ok) return 'solved';
                if (lastVerify.locked) return 'locked';
                return 'failed';
            }
            const st = modalState();
            if (st === 'locked') return 'locked';
            if (st === 'gone' && lastVerify && lastVerify.ok) return 'solved';
            await sleep(150);
        }
        if (lastVerify && lastVerify.at >= since) {
            return lastVerify.ok ? 'solved' : 'failed';
        }
        return modalState() === 'gone' ? 'gone' : 'timeout';
    }

    function clickSwalConfirm(re) {
        const btns = document.querySelectorAll('.swal2-confirm');
        for (const b of btns) {
            if (re.test((b.textContent || '').trim())) { b.click(); return true; }
        }
        return false;
    }

    async function runCaptcha(canvas) {
        captchaRunning = true;
        try {
            if (captchaRounds >= MAX_ROUNDS) {
                setCaptcha('stopped after ' + MAX_ROUNDS + ' attempts');
                return;
            }
            setCaptcha('reading puzzle…');
            await waitPainted(canvas);
            await sleep(450);                 // let the piece image finish loading

            let bg = await getBackground();
            let imgData, pw, ph, py, fromCanvas = false;
            if (bg) {
                imgData = bg.data; pw = bg.pw; ph = bg.ph; py = bg.py;
            } else {
                const ctx = canvas.getContext('2d');
                imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                pw = Math.floor(imgData.width * 0.125);
                ph = Math.floor(imgData.height * 0.2);
                const pd = lastPuzzleData;
                py = (pd && pd.piece_y != null)
                    ? Math.round(Number(pd.piece_y) * imgData.height / 250)
                    : Math.round(imgData.height * 0.4);
                fromCanvas = true;
            }

            const score = scoreDetect(imgData, pw, ph, py);
            const edges = detectCandidates(imgData, pw, ph, py);
            const ncc = (bg && bg.piece)
                ? nccDetect(imgData, bg.piece, pw, ph, py, 5, 6)
                : [];
            const cands = buildCandidates(score, ncc, edges).slice(0, 6);

            if (!cands.length) { setCaptcha('no slot detected'); return; }

            let outcome = 'timeout';
            for (let i = 0; i < Math.min(3, cands.length); i++) {
                const c = cands[i];
                setCaptcha('guess ' + (i + 1) + '/' + Math.min(3, cands.length) +
                    '  ' + c.pct.toFixed(2) + '%  (' + c.tag + ')' +
                    (fromCanvas ? ' canvas' : ''));
                lastVerify = null;
                const t = Date.now();
                if (!dragTo(c.pct)) { setCaptcha('slider not found'); return; }
                outcome = await waitForVerify(t, VERIFY_TIMEOUT);
                if (outcome === 'solved') {
                    setCaptcha('SOLVED ✓ server accepted ' + c.pct.toFixed(2) + '%');
                    return;
                }
                if (outcome === 'locked') { setCaptcha('locked out — wait ~10 min'); return; }
                if (outcome === 'failed') break;      // this guess is spent server-side
            }

            if (outcome === 'failed') captchaRounds++;
            if (captchaRounds >= MAX_ROUNDS) {
                setCaptcha('gave up after ' + MAX_ROUNDS + ' wrong guesses');
                return;
            }

            if (outcome === 'failed') {
                setCaptcha('wrong — new puzzle (' + (MAX_ROUNDS - captchaRounds) + ' left)');
                if (!clickSwalConfirm(/try\s*again|retry|tekrar/i)) clickSwalConfirm(/.*/);
            } else {
                setCaptcha('no answer from server — refreshing puzzle');
                const r = document.querySelector('.puzzle-refresh');
                if (r) r.click();
            }
        } catch (e) {
            console.error('[MathBot] captcha error', e);
            setCaptcha('error: ' + e.message);
        } finally {
            captchaRunning = false;
        }
    }

    /* ==================== game cycle (TRT) ========================== */

    const LS_KEY = 'ticarisk_bot_cfg_v1';
    const CFG_DEFAULTS = { cycleOn: true, scheduleOn: true, lastRun: '' };
    let cfg = Object.assign({}, CFG_DEFAULTS);
    try {
        cfg = Object.assign({}, CFG_DEFAULTS, JSON.parse(localStorage.getItem(LS_KEY) || '{}'));
    } catch (e) { cfg = Object.assign({}, CFG_DEFAULTS); }

    function saveCfg() {
        try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch (e) {}
    }

    // several tabs can hold a ticarisk page open at once — re-read the shared
    // copy before deciding to act, and take a lock so only one tab posts.
    function reloadCfg() {
        try {
            cfg = Object.assign({}, CFG_DEFAULTS, JSON.parse(localStorage.getItem(LS_KEY) || '{}'));
        } catch (e) { cfg = Object.assign({}, CFG_DEFAULTS); }
    }

    const BUSY_KEY = 'ticarisk_bot_busy_at';
    function acquireLock() {
        try {
            const last = Number(localStorage.getItem(BUSY_KEY) || 0);
            if (Date.now() - last < 120000) return false;
            localStorage.setItem(BUSY_KEY, String(Date.now()));
            return true;
        } catch (e) { return true; }
    }
    function releaseLock() {
        try { localStorage.removeItem(BUSY_KEY); } catch (e) {}
    }

    function setCycle(t) {
        const n = el('cycle-status'); if (n) n.textContent = t;
        console.log('[TicariskBot] ' + t);
    }
    function setClock(t) {
        const n = el('cycle-clock'); if (n) n.textContent = 'TRT ' + t;
    }
    function setMath(t) {
        const n = el('math-grind-status'); if (n) n.textContent = t;
        console.log('[TicariskBot] math: ' + t);
    }

    /* ------------------------- http helpers ------------------------- */

    function formBody(params) {
        const u = new URLSearchParams();
        for (const k in params) {
            const v = params[k];
            if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
            else if (v !== undefined && v !== null) u.append(k, v);
        }
        return u.toString();
    }

    async function postJson(path, params) {
        const r = await fetch(location.origin + path, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: formBody(params),
            credentials: 'same-origin'
        });
        const t = await r.text();
        try { return JSON.parse(t); } catch (e) { return { success: false, message: t.slice(0, 200) }; }
    }

    async function getHtml(path) {
        const r = await fetch(location.origin + path, {
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        return await r.text();
    }

    // mirrors bot.balance(): the header stat on index.php
    async function balanceOf() {
        try {
            const html = await getHtml('/index.php');
            const m = html.match(/nav-stat-balance'[^>]*data-value='(-?[\d.]+)'/);
            return m ? Number(m[1]) : null;
        } catch (e) { return null; }
    }

    async function getJson(path, params) {
        const r = await fetch(location.origin + path + '?' + formBody(params), {
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        const t = await r.text();
        try { return JSON.parse(t); } catch (e) { return { success: false, message: t.slice(0, 200) }; }
    }

    /* ------------------------- result parsing ----------------------- */

    const BENIGN_RE = new RegExp([
        'you (?:have|do not own|don\'t have)', 'no businesses', 'nothing (?:ready|to)',
        'are not ready', 'not waiting for watering', 'no suitable field found', 'no empty field',
        'no income or goods could be collected', 'no operations available',
        'all animals are already fed', 'no harvestable trees', 'no ready products', 'already fed',
        'honey is not ready'
    ].join('|'), 'i');
    const OK_ITEM_RE = new RegExp(
        'no harvestable trees|not ready|no ready products|already fed|not waiting|nothing to', 'i');
    function resText(res) {
        return String((res && (res.message || res.error)) || '');
    }

    let steps = { total: 0, bad: 0, warn: 0 };

    // mirrors bot.ok(): success flag, then benign-empty-state, then the
    // per-item "Errors:" list — only a non-benign item is a real failure.
    function step(section, action, res, softIsWarn) {
        const text = String((res && (res.message || res.error)) || '');
        let good = !!(res && res.success);
        let soft = false;
        if (!good && text.indexOf('Errors:') >= 0) {
            const items = text.split('Errors:')[1].split(',').map((s) => s.trim()).filter(Boolean);
            good = items.length > 0 && items.every((i) => OK_ITEM_RE.test(i));
        } else if (!good) {
            soft = /not enough materials|need: |already (?:producing|running)/i.test(text);
            good = soft ? false : BENIGN_RE.test(text);
            if (soft && softIsWarn === false) soft = false;
        }
        steps.total++;
        if (!good) { if (soft) steps.warn++; else steps.bad++; }
        const mark = good ? 'ok' : (soft ? 'warn' : 'FAIL');
        setCycle('[' + mark + '] ' + section + ': ' + action + (text ? ' — ' + text : ''));
        return good;
    }

    /* ---------------------------- parsers --------------------------- */

    function actionForms(src) {
        const out = [];
        const re = /<form[^>]*>[\s\S]*?<\/form>/g;
        let m;
        while ((m = re.exec(src || ''))) {
            const f = m[0];
            const b = f.match(/name="business_id"\s+value="(\d+)"/);
            const btn = f.match(/<button[^>]*name="([a-z_]+)"/);
            const tok = f.match(/name="csrf_token"\s+value="([0-9a-f]{64})"/);
            if (b && btn && tok) out.push({ bid: b[1], btn: btn[1], csrf: tok[1] });
        }
        return out;
    }

    function csrfOf(html) {
        const m = (html || '').match(/data-csrf="([0-9a-f]{64})"/);
        return m ? m[1] : '';
    }

    function fieldStates(html) {
        const out = {};
        const re = /data-tarla-id="(\d+)"[^>]*>/g;
        let m;
        while ((m = re.exec(html || ''))) {
            const d = html.slice(m.index, m.index + 300).match(/data-durum="([a-z_]+)"/);
            if (d) out[m[1]] = d[1];
        }
        if (Object.keys(out).length) return out;
        const re2 = /data-tarla-id="(\d+)"/g;
        while ((m = re2.exec(html || ''))) {
            const d = html.slice(m.index, m.index + 300).match(/data-durum="([a-z_]+)"/);
            if (d && !(m[1] in out)) out[m[1]] = d[1];
        }
        return out;
    }

    function ownedIds(html, attr) {
        const re = new RegExp('data-' + attr + '-id="(\\d+)"', 'g');
        const seen = Object.create(null);
        let m;
        while ((m = re.exec(html || ''))) seen[m[1]] = 1;
        return Object.keys(seen).sort((a, b) => Number(a) - Number(b));
    }

    // Page order matters for the rotation. A plain object keyed by crop id
    // would silently re-sort (integer-like keys enumerate ascending in JS),
    // so this returns an ordered list, not a map.
    function cropOptions(html) {
        for (const id of ['topluEkimUrun', 'urun_id']) {
            const box = (html || '').match(new RegExp('id="' + id + '"[\\s\\S]*?</select>'));
            if (!box) continue;
            const out = [];
            const seen = Object.create(null);
            const re = /<option value="(\d+)"[^>]*>\s*([^<]+)/g;
            let m;
            while ((m = re.exec(box[0]))) {
                const cid = Number(m[1]);
                const name = m[2].split(/\s*\(/)[0].trim();
                if (name && !seen[cid]) { seen[cid] = 1; out.push({ id: cid, name: name }); }
            }
            if (out.length) return out;
        }
        return [];
    }

    function plantedCrops(html) {
        const out = [];
        const re = /<h5 class="tarla-baslik">\s*([^<]+?)\s*<\/h5>/g;
        let m;
        while ((m = re.exec(html || ''))) {
            const name = m[1].replace(/\s+Planted\s*$/i, '').trim();
            if (name && !/^empty(field)?$/i.test(name)) out.push(name);
        }
        return out;
    }

    // Same rotation rule as bot.py: a cursor, and when there is none yet,
    // start after whatever crop is in the ground right now.
    function pickCrop(list, html) {
        if (!list.length) return { id: CROP_ID, label: String(CROP_ID), why: 'no crop list' };
        if (!CROP_ROTATION) {
            let i = list.findIndex((c) => c.id === CROP_ID);
            if (i < 0) i = 0;
            return { id: list[i].id, label: list[i].name, why: 'rotation off' };
        }

        const raw = localStorage.getItem(CROP_KEY);
        let i, why;
        if (raw !== null && raw !== '' && Number.isFinite(Number(raw))) {
            i = ((Number(raw) % list.length) + list.length) % list.length;
            why = 'stored cursor';
        } else {
            const grown = plantedCrops(html);
            const names = list.map((c) => c.name);
            if (grown.length) {
                const counts = Object.create(null);
                grown.forEach((n) => { counts[n] = (counts[n] || 0) + 1; });
                const common = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
                const pos = names.indexOf(common);
                i = pos >= 0 ? (pos + 1) % names.length : 0;
                why = 'seeded after ' + common;
            } else {
                i = 0;
                why = 'seeded (nothing growing)';
            }
        }
        try { localStorage.setItem(CROP_KEY, String((i + 1) % list.length)); } catch (e) {}
        return { id: list[i].id, label: list[i].name, why: why };
    }

    // A crop the field's level will not accept; anything else is a different problem.
    const LEVEL_BLOCKED_RE = /level|seviye|unlock|locked|not (?:yet )?available|Sv\.\d|require/i;

    function animalTasks(html, idAttr) {
        const out = [];
        const seen = Object.create(null);
        const re = /<[^>]+data-hayvan-turu="([^"]+)"[^>]*>/g;
        let m;
        while ((m = re.exec(html || ''))) {
            const tag = m[0];
            const h = tag.match(new RegExp('data-' + idAttr + '-id="(\\d+)"'));
            if (!h) continue;
            const ut = tag.match(/data-urun-tipi="([^"]*)"/);
            const urun = ut ? ut[1] : '';
            const key = h[1] + '|' + m[1] + '|' + urun;
            if (seen[key]) continue;
            seen[key] = 1;
            out.push({ hid: h[1], tur: m[1], urun });
        }
        return out;
    }

    /* ------------------------- section drivers ---------------------- */

    async function cycleProduction() {
        let page = await getHtml('/businesses.php');
        let r = await postJson('/businesses.php', {
            action: 'toplu_tum_isletme_topla', csrf_token: csrfOf(page)
        });
        step('production', 'collect all', r);

        page = await getHtml('/businesses.php');
        const owned = await getJson('/businesses.php', { ajax_lazy: 'owned_bundle' });
        const forms = [
            ...actionForms(page),
            ...actionForms(owned.factories || ''),
            ...actionForms(owned.food || '')
        ];
        const starts = forms.filter((f) => f.btn === 'start_production');
        for (const f of starts) {
            r = await postJson('/businesses.php', {
                csrf_token: f.csrf, business_id: f.bid, start_production: '1'
            });
            step('production', 'start ' + f.bid, r);
        }
        if (!starts.length) step('production', 'restart', { success: true, message: 'no idle slot' });
    }

    async function cycleFields() {
        await postJson('/tarlalar.php', { ajax_request: 1, check_tarla_status: 1 });
        let html = await getHtml('/tarlalar.php');
        let st = fieldStates(html);
        const ids = Object.keys(st);
        if (!ids.length) { step('fields', 'scan', { success: true, message: 'no fields owned' }); return; }

        const ready = ids.filter((i) => ['hasat_hazir', 'hazir', 'ready'].indexOf(st[i]) >= 0);
        if (ready.length) {
            step('fields', 'harvest ' + ready.length, await postJson('/tarlalar.php', {
                ajax_request: 1, topu_hasat: 1, 'tarla_ids[]': ready
            }));
        } else {
            step('fields', 'harvest', { success: true, message: 'nothing ready' });
        }

        html = await getHtml('/tarlalar.php');
        st = fieldStates(html);
        const empty = Object.keys(st).filter((i) => ['', 'bos', 'empty'].indexOf(st[i]) >= 0);
        if (!empty.length) { step('fields', 'replant', { success: true, message: 'no empty field' }); return; }

        const crops = cropOptions(html);
        let pick = pickCrop(crops, html);

        const plant = (cid) => postJson('/tarlalar.php', {
            ajax_request: 1, topu_ekim: 1, urun_id: cid, 'tarla_ids[]': empty
        });

        let res = await plant(pick.id);
        let good = step('fields',
            'plant ' + pick.label + ' on ' + empty.length + ' (' + pick.why + ')', res);
        let text = resText(res);

        // a crop above this field's Sv level is refused — walk the rotation
        // forward until the server takes one instead of getting stuck on it
        const tried = [pick.id];
        while (!good && crops.length && LEVEL_BLOCKED_RE.test(text)) {
            const nxt = crops.filter((c) => tried.indexOf(c.id) < 0)[0];
            if (!nxt) break;
            tried.push(nxt.id);
            pick = { id: nxt.id, label: nxt.name, why: 'rotation fallback' };
            res = await plant(pick.id);
            good = step('fields', 'plant ' + pick.label + ' (rotation fallback)', res);
            text = resText(res);
        }

        if (!good && /water/i.test(text)) {
            if (await maybeBuyWater()) {
                res = await plant(pick.id);
                step('fields', 'replant ' + pick.label, res);
            }
        }
    }

    // mirrors bot._maybe_buy_water(); BUY_WATER_LITERS=0 keeps it inert
    async function maybeBuyWater() {
        if (BUY_WATER_LITERS <= 0) {
            step('fields', 'buy water', { success: true, message: 'skipped (BUY_WATER_LITERS=0)' });
            return false;
        }
        const page = await getHtml('/hammaddeler.php');
        const m = page.match(/data-material-id="su"[\s\S]*?data-price="(\d+)"[\s\S]*?data-csrf="([0-9a-f]{64})"/);
        if (!m) { step('fields', 'buy water', { success: false, message: 'water listing not found' }); return false; }
        const price = Number(m[1]);
        let qty = BUY_WATER_LITERS;
        if (MAX_WATER_SPEND > 0) qty = Math.min(qty, Math.floor(MAX_WATER_SPEND / Math.max(price, 1)));
        const bal = await balanceOf();
        if (bal !== null && bal !== undefined) qty = Math.min(qty, Math.floor(bal / Math.max(price, 1)));
        if (qty <= 0) { step('fields', 'buy water', { success: false, message: 'cannot afford any' }); return false; }
        const res = await postJson('/hammaddeler.php', {
            csrf_token: m[2], material_id: 'su', quantity: qty, buy_material: '1'
        });
        const good = step('fields', 'buy ' + qty + ' L water', res);
        return good;
    }

    async function cycleOrchards() {
        await postJson('/bahceler.php', { ajax_request: 1, check_plants_status: 1 });
        const html = await getHtml('/bahceler.php');
        const ids = ownedIds(html, 'bahce');
        if (!ids.length) { step('orchards', 'scan', { success: true, message: 'no orchards owned' }); return; }
        step('orchards', 'harvest ' + ids.length, await postJson('/bahceler.php', {
            ajax_request: 1, topu_meyve_topla_coklu: 1, 'bahce_ids[]': ids
        }));
        step('orchards', 'water ' + ids.length, await postJson('/bahceler.php', {
            ajax_request: 1, topu_bahce_sula: 1, 'bahce_ids[]': ids
        }));
    }

    async function cycleAnimals(section, path, attr) {
        const html = await getHtml('/' + path + '.php');
        const ids = ownedIds(html, attr);
        if (!ids.length) { step(section, 'scan', { success: true, message: 'no ' + section + ' owned' }); return; }

        const tasks = animalTasks(html, attr);
        if (tasks.length) {
            for (const t of tasks) {
                const p = { ajax_request: 1, topu_urun_topla: 1 };
                p[attr + '_id'] = t.hid;
                p.hayvan_turu = t.tur;
                if (t.urun || attr === 'ahir') p.urun_tipi = t.urun;
                step(section, 'collect ' + t.hid + '/' + t.tur, await postJson('/' + path + '.php', p));
                if (FEED_ANIMALS) {
                    const f = { ajax_request: 1, topu_besle: 1, hayvan_turu: t.tur };
                    f[attr + '_id'] = t.hid;
                    step(section, 'feed ' + t.hid + '/' + t.tur, await postJson('/' + path + '.php', f));
                }
            }
            return;
        }
        if (!FEED_ANIMALS) {
            step(section, 'collect', { success: true, message: 'feeding disabled' });
            return;
        }
        for (const id of ids) {
            const p = { ajax_request: 1, tum_islemler: 1 };
            p[attr + '_id'] = id;
            step(section, 'feed+collect ' + id, await postJson('/' + path + '.php', p));
        }
    }

    // aricilik_id -> {hives, petek, max_petek}. The ids exist only inside the
    // buy-button onclick handlers; there is no data-aricilik-id attribute.
    function beeAreas(html) {
        const out = Object.create(null);
        const need = (i) => out[i] || (out[i] = { hives: 0, petek: 0, max_petek: 0 });
        let m;
        const reK = /kovanAlModal\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/g;
        while ((m = reK.exec(html || ''))) need(Number(m[1])).hives += Number(m[2]);
        const reP = /petekAlModal\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/g;
        while ((m = reP.exec(html || ''))) {
            const d = need(Number(m[1]));
            d.hives = Math.max(d.hives, Number(m[2]));
            d.petek = Number(m[3]);
        }
        Object.keys(out).forEach((k) => { out[k].max_petek = out[k].hives * 10; });
        return out;
    }

    async function cycleBees() {
        let html = await getHtml('/aricilik.php');
        let areas = beeAreas(html);
        const ids = Object.keys(areas).map(Number);
        if (!ids.length) { step('bees', 'scan', { success: true, message: 'no beekeeping area owned' }); return; }

        for (const id of ids) {
            step('bees', 'harvest ' + id, await postJson('/aricilik.php', {
                ajax_request: 1, bal_hasat: 1, aricilik_id: id
            }));
        }

        if (!PETEK_REFILL) {
            step('bees', 'refill comb', { success: true, message: 'skipped (PETEK_REFILL=false)' });
            return;
        }

        // harvesting burns the comb — re-read so we refill against the real count
        html = await getHtml('/aricilik.php');
        areas = beeAreas(html);
        for (const k of Object.keys(areas).map(Number).sort((a, b) => a - b)) {
            const st = areas[k];
            const want = Math.max(0, st.max_petek - st.petek);
            if (want <= 0) {
                step('bees', 'comb ' + k, { success: true, message: 'full (' + st.petek + '/' + st.max_petek + ')' });
                continue;
            }
            const cost = want * PETEK_PRICE;
            if (cost > PETEK_MAX_SPEND) {
                step('bees', 'buy ' + want + ' comb', {
                    success: false,
                    message: '$' + cost.toLocaleString() + ' exceeds PETEK_MAX_SPEND=$' + PETEK_MAX_SPEND.toLocaleString()
                }, true);
                continue;
            }
            const bal = await balanceOf();
            if (bal !== null && cost > bal) {
                step('bees', 'buy ' + want + ' comb', {
                    success: false, message: 'cost $' + cost.toLocaleString() + ' is above the $' + bal.toFixed(2) + ' balance'
                }, true);
                continue;
            }
            step('bees', 'buy ' + want + ' comb ($' + cost.toLocaleString() + ')',
                await postJson('/aricilik.php', {
                    ajax_request: 1, petek_ekle: 1, aricilik_id: k, petek_adet: want
                }));
        }
    }

    /* --------------------------- math grind ------------------------- */

    async function waitForCaptcha(ms) {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
            if (!captchaRunning && !document.getElementById('puzzleCanvas')) return true;
            await sleep(400);
        }
        return false;
    }

    // Solve every question the server will hand out, then move the vault over.
    async function mathGrind(tag) {
        if (mathBusy) { setMath('grind already running'); return; }
        if (lastVerify && lastVerify.locked) { setMath('captcha lock active — skipping for now'); return; }
        mathBusy = true;
        const t0 = Date.now();
        let right = 0, wrong = 0, left = null, why = '';
        setMath((tag ? tag + ' — ' : '') + 'starting…');
        try {
            if (!worker) { try { await initOCR(); } catch (e) { console.error('[MathBot] OCR init', e); } }
            for (let i = 0; i < MATH_MAX_ROUNDS; i++) {
                const q = await postJson('/matematik.php', { action: 'get_question', islem_tipi: 'toplama' });
                if (!q || !q.success) { why = 'no question: ' + resText(q); break; }

                const r = await readQuestion(q.question_image_url, q.islem_tipi);
                if (!r) { why = 'OCR failed — stopping rather than guessing'; break; }

                await sleep(MIN_ANSWER_MS);   // server rejects faster than ~1.4s
                const a = await postJson('/matematik.php', {
                    action: 'check_answer', cevap: String(r.ans), token: q.token
                });
                if (a && a.dogru) right++; else wrong++;
                if (a && a.kalan_islem !== undefined && a.kalan_islem !== null) left = Number(a.kalan_islem);

                if (a && a.verification_required) {
                    setMath('captcha — solving…');
                    if (!(await waitForCaptcha(45000))) { why = 'captcha not cleared'; break; }
                }

                if (left !== null) {
                    setMath('grind: ' + right + ' right, ' + wrong + ' wrong — ' + left + ' left');
                    if (left <= 0) break;
                }
                await sleep(400);
            }

            let vault = '';
            if (MATH_WITHDRAW) {
                const w = await postJson('/matematik.php', { action: 'hesaba_cek' });
                vault = w && w.success ? ' — vault transferred'
                    : (w ? ' — vault: ' + resText(w) : '');
            }
            setMath('done ' + right + '/' + (right + wrong) + ' in ' +
                Math.round((Date.now() - t0) / 1000) + 's' +
                (left !== null ? ', ' + left + ' left' : '') + vault +
                (why ? ' (' + why + ')' : ''));
        } catch (e) {
            console.error('[TicariskBot] math grind error', e);
            setMath('error: ' + e.message);
        } finally {
            mathBusy = false;
        }
    }

    let cycleBusy = false;

    async function runCycle() {
        if (cycleBusy) { setCycle('cycle already running'); return; }
        if (!acquireLock()) { setCycle('another tab is already running a cycle'); return; }
        cycleBusy = true;
        steps = { total: 0, bad: 0, warn: 0 };
        const t0 = Date.now();
        setCycle('cycle running…');
        try {
            const only = location.pathname;
            if (CYCLE_SECTIONS.indexOf('production') >= 0) await cycleProduction();
            if (CYCLE_SECTIONS.indexOf('fields') >= 0) await cycleFields();
            if (CYCLE_SECTIONS.indexOf('orchards') >= 0) await cycleOrchards();
            if (CYCLE_SECTIONS.indexOf('barns') >= 0) await cycleAnimals('barns', 'ahirlar', 'ahir');
            if (CYCLE_SECTIONS.indexOf('coops') >= 0) await cycleAnimals('coops', 'kumesler', 'kumes');
            if (CYCLE_SECTIONS.indexOf('bees') >= 0) await cycleBees();
            const secs = Math.round((Date.now() - t0) / 1000);
            setCycle('cycle done — ' + (steps.total - steps.bad - steps.warn) + '/' + steps.total +
                ' ok, ' + steps.warn + ' warn, ' + secs + 's  (' + only + ')');
            if (MATH_GRIND) await mathGrind('after farm cycle');
        } catch (e) {
            console.error('[TicariskBot] cycle error', e);
            setCycle('cycle error: ' + e.message);
        } finally {
            cycleBusy = false;
            releaseLock();
        }
    }

    /* --------------------------- Turkish time ----------------------- */

    function istanbul() {
        try {
            const p = new Intl.DateTimeFormat('en-GB', {
                timeZone: 'Europe/Istanbul', year: 'numeric', month: '2-digit',
                day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
            }).formatToParts(new Date());
            const g = (t) => (p.find((x) => x.type === t) || {}).value || '00';
            const hour = g('hour'), minute = g('minute');
            return { key: g('year') + g('month') + g('day') + '-' + hour, hour, minute };
        } catch (e) {
            const d = new Date();
            const p = (n) => String(n).padStart(2, '0');
            return { key: p(d.getFullYear()) + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()), hour: p(d.getHours()), minute: p(d.getMinutes()) };
        }
    }

    const TARGET_MIN = String(SCHEDULE_MINUTE).padStart(2, '0');

    function tick() {
        reloadCfg();
        const t = istanbul();
        setClock(t.hour + ':' + t.minute);
        if (!cfg.cycleOn || !cfg.scheduleOn || cycleBusy) return;
        if (t.minute !== TARGET_MIN) return;
        if (cfg.lastRun === t.key) return;
        cfg.lastRun = t.key;
        saveCfg();
        runCycle();
    }

    // PC just came on after the slot passed — take the missed run now.
    function catchUp() {
        reloadCfg();
        const t = istanbul();
        if (!cfg.cycleOn || !cfg.scheduleOn) return;
        if (cfg.lastRun === t.key) return;
        if (Number(t.minute) < SCHEDULE_MINUTE) return;
        cfg.lastRun = t.key;
        saveCfg();
        setTimeout(() => runCycle(), 9000);
    }

    function startScheduler() {
        setInterval(tick, 15000);
        tick();
        setTimeout(catchUp, 6000);
    }

    /* ---------------------------- self-update ----------------------- */

    function cmpVer(a, b) {
        const pa = String(a).split('.').map(Number);
        const pb = String(b).split('.').map(Number);
        for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
            const d = (pa[i] || 0) - (pb[i] || 0);
            if (d) return d;
        }
        return 0;
    }

    async function checkForUpdate() {
        if (!UPDATE_URL) { setCycle('set UPDATE_URL (and @downloadURL/@updateURL) to enable updates'); return false; }
        try {
            const r = await fetch(UPDATE_URL + (UPDATE_URL.indexOf('?') < 0 ? '?' : '&') + 't=' + Date.now(), { cache: 'no-store' });
            const txt = await r.text();
            const m = txt.match(/@version\s+([0-9][\w.]*)/);
            if (!m) { setCycle('update check failed — no @version found'); return false; }
            if (cmpVer(m[1], VERSION) > 0) {
                setCycle('UPDATE AVAILABLE: v' + m[1] + ' > v' + VERSION + ' — reinstall the script');
                return true;
            }
            setCycle('up to date (v' + VERSION + ')');
            return false;
        } catch (e) {
            setCycle('update check error: ' + e.message);
            return false;
        }
    }

    /* ============================ startup =========================== */

    function boot() {
        createUI();
        // OCR is only needed for the math page — don't load tesseract everywhere
        if (/matematik\.php$/.test(location.pathname)) initOCR();
        else setStatus('OCR idle — loads on matematik.php');
        watchModal();
        startScheduler();
        if (UPDATE_URL) setTimeout(() => checkForUpdate(), 20000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
