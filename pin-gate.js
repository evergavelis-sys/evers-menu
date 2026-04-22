/* pin-gate.js — tiny 4-digit PIN gate for Ever's Menu.
 *
 * How it works:
 *   • An inline <script> in <head> adds `pin-locked` to <html> pre-parse,
 *     and CSS hides <body> while that class is present. That avoids any
 *     flash of the app before the gate mounts.
 *   • Once DOM is parsed, this script either shows the overlay or
 *     removes the lock class if a valid unlock token is cached.
 *   • Unlock persists in localStorage. Clear it by visiting ?reset
 *     (existing handler in index.html already wipes localStorage).
 *
 * Change the PIN by editing PIN below. Not a security boundary —
 * a passerby deterrent, nothing more. Anyone who views source sees it.
 */
(function () {
  'use strict';

  var PIN = '4242';
  var KEY = 'evers-menu:pin-unlocked:v1';
  var ROOT = document.documentElement;

  function unlock() {
    try { localStorage.setItem(KEY, '1'); } catch (e) {}
    ROOT.classList.remove('pin-locked');
    var el = document.getElementById('pin-gate');
    if (el) el.remove();
  }

  function alreadyUnlocked() {
    try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
  }

  function mountOverlay() {
    // Styles inlined so this works even if the app CSS is still loading.
    var css = [
      '#pin-gate{position:fixed;inset:0;z-index:2147483647;background:#000;',
      'display:flex;flex-direction:column;align-items:center;justify-content:center;',
      'font-family:-apple-system,BlinkMacSystemFont,"Jost",system-ui,sans-serif;',
      'color:#f0ede8;padding:env(safe-area-inset-top) 1rem env(safe-area-inset-bottom);}',
      '#pin-gate .pg-title{font-size:22px;font-weight:800;color:#b04a8a;letter-spacing:-0.3px;margin-bottom:6px;}',
      '#pin-gate .pg-sub{font-size:12px;color:#7a7672;margin-bottom:28px;letter-spacing:1.2px;text-transform:uppercase;}',
      '#pin-gate .pg-dots{display:flex;gap:14px;margin-bottom:32px;}',
      '#pin-gate .pg-dot{width:14px;height:14px;border-radius:50%;border:1.5px solid rgba(255,255,255,0.25);transition:background 120ms,border-color 120ms;}',
      '#pin-gate .pg-dot.filled{background:#b04a8a;border-color:#b04a8a;}',
      '#pin-gate.pg-error .pg-dot{border-color:#c4705a;animation:pg-shake 320ms;}',
      '@keyframes pg-shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}',
      '#pin-gate .pg-pad{display:grid;grid-template-columns:repeat(3,72px);gap:10px;}',
      '#pin-gate .pg-key{height:72px;border-radius:50%;border:1px solid rgba(255,255,255,0.14);',
      'background:transparent;color:#f0ede8;font-size:26px;font-weight:400;font-family:inherit;cursor:pointer;',
      '-webkit-tap-highlight-color:transparent;}',
      '#pin-gate .pg-key:active{background:#1a1a1a;}',
      '#pin-gate .pg-key.pg-del{font-size:14px;color:#7a7672;border-color:transparent;}',
      '#pin-gate .pg-key.pg-empty{visibility:hidden;}',
      '#pin-gate .pg-hint{margin-top:22px;font-size:11px;color:#4a4744;letter-spacing:0.4px;}'
    ].join('');
    var style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    var host = document.createElement('div');
    host.id = 'pin-gate';
    host.setAttribute('role', 'dialog');
    host.setAttribute('aria-label', 'Enter PIN');
    host.innerHTML =
      '<div class="pg-title">Ever\u2019s Menu</div>' +
      '<div class="pg-sub">Enter PIN</div>' +
      '<div class="pg-dots">' +
        '<div class="pg-dot"></div><div class="pg-dot"></div>' +
        '<div class="pg-dot"></div><div class="pg-dot"></div>' +
      '</div>' +
      '<div class="pg-pad">' +
        '<button class="pg-key" data-k="1">1</button>' +
        '<button class="pg-key" data-k="2">2</button>' +
        '<button class="pg-key" data-k="3">3</button>' +
        '<button class="pg-key" data-k="4">4</button>' +
        '<button class="pg-key" data-k="5">5</button>' +
        '<button class="pg-key" data-k="6">6</button>' +
        '<button class="pg-key" data-k="7">7</button>' +
        '<button class="pg-key" data-k="8">8</button>' +
        '<button class="pg-key" data-k="9">9</button>' +
        '<button class="pg-key pg-empty"></button>' +
        '<button class="pg-key" data-k="0">0</button>' +
        '<button class="pg-key pg-del" data-k="del" aria-label="delete">\u232B</button>' +
      '</div>' +
      '<div class="pg-hint">tap to unlock</div>';
    document.body.appendChild(host);

    var entered = '';
    var dots = host.querySelectorAll('.pg-dot');

    function renderDots() {
      host.classList.remove('pg-error');
      for (var i = 0; i < dots.length; i++) {
        dots[i].classList.toggle('filled', i < entered.length);
      }
    }

    function fail() {
      host.classList.add('pg-error');
      setTimeout(function () {
        entered = '';
        renderDots();
      }, 340);
    }

    function press(k) {
      if (k === 'del') {
        entered = entered.slice(0, -1);
        renderDots();
        return;
      }
      if (entered.length >= 4) return;
      entered += k;
      renderDots();
      if (entered.length === 4) {
        if (entered === PIN) {
          setTimeout(unlock, 120);
        } else {
          fail();
        }
      }
    }

    host.addEventListener('click', function (e) {
      var btn = e.target.closest('.pg-key');
      if (!btn || !btn.dataset.k) return;
      press(btn.dataset.k);
    });

    document.addEventListener('keydown', function onKey(e) {
      if (!document.getElementById('pin-gate')) {
        document.removeEventListener('keydown', onKey);
        return;
      }
      if (e.key >= '0' && e.key <= '9') press(e.key);
      else if (e.key === 'Backspace') press('del');
    });
  }

  function boot() {
    if (alreadyUnlocked()) {
      ROOT.classList.remove('pin-locked');
      return;
    }
    mountOverlay();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
