var LuxWalkthrough = (function() {
  var steps = [];
  var currentStep = 0;
  var walkthroughId = null;
  var overlay = null;
  var popup = null;
  var highlight = null;

  function init() {
    overlay = document.createElement('div');
    overlay.className = 'lux-wt-overlay';
    overlay.style.display = 'none';
    document.body.appendChild(overlay);

    highlight = document.createElement('div');
    highlight.className = 'lux-wt-highlight';
    highlight.style.display = 'none';
    document.body.appendChild(highlight);

    popup = document.createElement('div');
    popup.className = 'lux-wt-popup';
    popup.style.display = 'none';
    document.body.appendChild(popup);
  }

  function start(id, stepDefs) {
    walkthroughId = id;
    steps = stepDefs || [];
    currentStep = 0;
    if (steps.length === 0) return;
    LuxHelp.closeDrawer();
    overlay.style.display = 'block';
    showStep();
  }

  function showStep() {
    if (currentStep >= steps.length) {
      complete();
      return;
    }
    var step = steps[currentStep];
    var el = step.selector ? document.querySelector(step.selector) : null;

    if (el) {
      var rect = el.getBoundingClientRect();
      highlight.style.display = 'block';
      highlight.style.top = (rect.top + window.scrollY - 4) + 'px';
      highlight.style.left = (rect.left + window.scrollX - 4) + 'px';
      highlight.style.width = (rect.width + 8) + 'px';
      highlight.style.height = (rect.height + 8) + 'px';
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      highlight.style.display = 'none';
    }

    var isRequired = step.required ? '<span class="lux-wt-required">Required</span>' : '';
    var skipBtn = step.required ? '' : '<button class="lux-wt-btn lux-wt-btn-skip" onclick="LuxWalkthrough.skip()">Skip</button>';

    popup.innerHTML =
      '<div class="lux-wt-popup-header">' +
        '<span class="lux-wt-step-count">Step ' + (currentStep + 1) + ' of ' + steps.length + '</span>' +
        isRequired +
        '<button class="lux-wt-close" onclick="LuxWalkthrough.end()">&times;</button>' +
      '</div>' +
      '<h4>' + (step.title || '') + '</h4>' +
      '<p>' + (step.description || '') + '</p>' +
      '<div class="lux-wt-progress">' +
        '<div class="lux-wt-progress-fill" style="width:' + ((currentStep + 1) / steps.length * 100) + '%"></div>' +
      '</div>' +
      '<div class="lux-wt-actions">' +
        (currentStep > 0 ? '<button class="lux-wt-btn lux-wt-btn-back" onclick="LuxWalkthrough.back()">Back</button>' : '') +
        skipBtn +
        '<button class="lux-wt-btn lux-wt-btn-next" onclick="LuxWalkthrough.next()">' +
          (currentStep === steps.length - 1 ? 'Finish' : 'Next') +
        '</button>' +
      '</div>';
    popup.style.display = 'block';

    if (el) {
      var rect = el.getBoundingClientRect();
      var popupTop = rect.bottom + window.scrollY + 12;
      var popupLeft = Math.max(16, rect.left + window.scrollX - 20);
      if (popupTop + 200 > window.innerHeight + window.scrollY) {
        popupTop = rect.top + window.scrollY - popup.offsetHeight - 12;
      }
      if (popupLeft + 340 > window.innerWidth) {
        popupLeft = window.innerWidth - 356;
      }
      popup.style.top = popupTop + 'px';
      popup.style.left = popupLeft + 'px';
    } else {
      popup.style.top = '50%';
      popup.style.left = '50%';
      popup.style.transform = 'translate(-50%, -50%)';
    }
  }

  function next() {
    var step = steps[currentStep];
    if (step.required && step.validate) {
      var el = document.querySelector(step.selector);
      if (el && !el.value && !el.textContent.trim()) {
        popup.querySelector('.lux-wt-popup-header').insertAdjacentHTML(
          'afterend', '<div class="lux-wt-error">This step must be completed before continuing.</div>'
        );
        return;
      }
    }
    markStepComplete(currentStep);
    currentStep++;
    showStep();
  }

  function back() {
    if (currentStep > 0) {
      currentStep--;
      showStep();
    }
  }

  function skip() {
    currentStep++;
    showStep();
  }

  function end() {
    overlay.style.display = 'none';
    highlight.style.display = 'none';
    popup.style.display = 'none';
    popup.style.transform = '';
    steps = [];
    currentStep = 0;
  }

  function complete() {
    end();
    if (walkthroughId) {
      var csrf = document.querySelector('meta[name="csrf-token"]');
      fetch('/api/walkthroughs/' + walkthroughId + '/complete', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf ? csrf.content : ''
        }
      }).catch(function() {});
    }
    showCompletionToast();
  }

  function markStepComplete(idx) {
    if (!walkthroughId) return;
    var csrf = document.querySelector('meta[name="csrf-token"]');
    fetch('/api/walkthroughs/' + walkthroughId + '/step', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf ? csrf.content : ''
      },
      body: JSON.stringify({ step_index: idx })
    }).catch(function() {});
  }

  function showCompletionToast() {
    var toast = document.createElement('div');
    toast.className = 'lux-wt-toast';
    toast.innerHTML =
      '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#00e5ff" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
      '<span>Walkthrough complete!</span>';
    document.body.appendChild(toast);
    setTimeout(function() { toast.classList.add('lux-wt-toast-show'); }, 50);
    setTimeout(function() {
      toast.classList.remove('lux-wt-toast-show');
      setTimeout(function() { toast.remove(); }, 300);
    }, 3000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { start: start, next: next, back: back, skip: skip, end: end };
})();

var LuxHelp = (function() {
  var currentScreenKey = null;

  function openDrawer(screenKey) {
    currentScreenKey = screenKey;
    var drawer = document.getElementById('lux-help-drawer');
    if (!drawer) return;
    drawer.classList.add('open');
    document.body.style.overflow = 'hidden';
    loadContent(screenKey);
  }

  function closeDrawer() {
    var drawer = document.getElementById('lux-help-drawer');
    if (!drawer) return;
    drawer.classList.remove('open');
    document.body.style.overflow = '';
  }

  function loadContent(screenKey) {
    var loading = document.getElementById('help-loading');
    var content = document.getElementById('help-content-section');
    var empty = document.getElementById('help-empty');
    loading.style.display = 'flex';
    content.style.display = 'none';
    empty.style.display = 'none';

    fetch('/api/help/' + encodeURIComponent(screenKey))
      .then(function(r) { return r.json(); })
      .then(function(data) {
        loading.style.display = 'none';
        if (!data.help || data.help.length === 0) {
          empty.style.display = 'flex';
          return;
        }
        content.style.display = 'block';
        var h = data.help[0];
        document.getElementById('help-drawer-title').textContent = h.title || 'Help';
        document.getElementById('help-instructions').innerHTML = h.instructions || '';

        var videoSec = document.getElementById('help-video-section');
        if (h.video_url) {
          videoSec.style.display = 'block';
          document.getElementById('help-video-embed').innerHTML =
            '<iframe src="' + h.video_url + '" frameborder="0" allowfullscreen style="width:100%;aspect-ratio:16/9;border-radius:8px;"></iframe>';
        } else {
          videoSec.style.display = 'none';
        }

        var pdfSec = document.getElementById('help-pdf-section');
        if (h.pdf_url) {
          pdfSec.style.display = 'block';
          document.getElementById('help-pdf-link').href = h.pdf_url;
        } else {
          pdfSec.style.display = 'none';
        }

        if (data.walkthroughs && data.walkthroughs.length > 0) {
          var wtSec = document.getElementById('help-walkthroughs-section');
          wtSec.style.display = 'block';
          var list = document.getElementById('help-walkthroughs-list');
          list.innerHTML = '';
          data.walkthroughs.forEach(function(wt) {
            var btn = document.createElement('button');
            btn.className = 'lux-help-wt-btn';
            btn.innerHTML =
              '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>' +
              '<span>' + wt.name + '</span>';
            btn.onclick = function() {
              LuxWalkthrough.start(wt.id, wt.steps);
            };
            list.appendChild(btn);
          });
        } else {
          document.getElementById('help-walkthroughs-section').style.display = 'none';
        }
      })
      .catch(function() {
        loading.style.display = 'none';
        empty.style.display = 'flex';
      });
  }

  function shareHelp() {
    if (currentScreenKey) {
      var url = window.location.origin + window.location.pathname + '?help=' + currentScreenKey;
      navigator.clipboard.writeText(url).then(function() {
        alert('Help link copied to clipboard!');
      }).catch(function() {
        prompt('Copy this link:', url);
      });
    }
  }

  function printHelp() {
    var body = document.getElementById('help-drawer-body');
    if (!body) return;
    var w = window.open('', '_blank');
    w.document.write('<html><head><title>Help</title><style>body{font-family:system-ui,sans-serif;padding:2rem;color:#333;}h3,h4{color:#6b21a8;}a{color:#7c3aed;}</style></head><body>');
    w.document.write(body.innerHTML);
    w.document.write('</body></html>');
    w.document.close();
    w.print();
  }

  function loadOnboardingProgress() {
    var widget = document.getElementById('onboarding-progress-widget');
    if (!widget) return;
    fetch('/api/onboarding/progress')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.progress) return;
        var p = data.progress;
        document.getElementById('onb-setup-pct').textContent = p.setup_pct + '%';
        document.getElementById('onb-setup-fill').style.width = p.setup_pct + '%';
        document.getElementById('onb-training-pct').textContent = p.training_pct + '%';
        document.getElementById('onb-training-fill').style.width = p.training_pct + '%';
        document.getElementById('onb-docs-pct').textContent = p.docs_pct + '%';
        document.getElementById('onb-docs-fill').style.width = p.docs_pct + '%';
        var overall = Math.round((p.setup_pct + p.training_pct + p.docs_pct) / 3);
        document.getElementById('onb-overall-pct').textContent = overall + '%';
        var golive = document.getElementById('onb-golive-status');
        if (p.go_live_ready) {
          golive.className = 'lux-onb-goLive lux-onb-ready';
          golive.querySelector('strong').textContent = 'Ready!';
        }
      })
      .catch(function() {});
  }

  function checkAutoOpen() {
    var params = new URLSearchParams(window.location.search);
    var helpKey = params.get('help');
    if (helpKey) {
      openDrawer(helpKey);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      checkAutoOpen();
      loadOnboardingProgress();
    });
  } else {
    checkAutoOpen();
    loadOnboardingProgress();
  }

  return { openDrawer: openDrawer, closeDrawer: closeDrawer, shareHelp: shareHelp, printHelp: printHelp, loadOnboardingProgress: loadOnboardingProgress };
})();
