/* app.js — RoadWatch.AI PWA — Live Supabase Edition */
'use strict';

// ── Supabase config (anon key is safe to expose — RLS protects data) ──
const SUPABASE_URL = 'https://fbjjoktuzirhpqqpzfbo.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZiampva3R1emlyaHBxcXB6ZmJvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU0MjA1MzcsImV4cCI6MjEwMDk5NjUzN30.sp9Kgpt7alImqzhzWkWo1Gx4FTzut0Fzm9IPu8fX0po';

// ── Backend API URL — auto-switches between local dev and deployed Render ──
// When deployed to Vercel, update the RENDER_URL below to your Render service URL.
const RENDER_URL = 'https://roadwatch-backend.onrender.com';
const API_BASE = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
  ? 'http://localhost:8000'
  : RENDER_URL;

// Lazy-load Supabase JS from CDN (loaded in index.html <head>)
let _sb = null;
function getSB() {
  if (_sb) return _sb;
  if (window.supabase) {
    _sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    return _sb;
  }
  return null;
}

// Check immediately (synchronously) if Supabase session is stored in localStorage
function hasSavedSupabaseSession() {
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('sb-') && k.endsWith('-auth-token')) {
        const v = localStorage.getItem(k);
        if (v && v.includes('access_token')) return true;
      }
    }
  } catch (e) {}
  return false;
}

function enterApp() {
  splashEnded = true;
  const splashEl = document.getElementById('splash');
  if (splashEl) {
    splashEl.style.display = 'none';
    splashEl.style.opacity = '0';
  }
  // Strictly hide login screen and cursor grid
  if (loginScreen) {
    loginScreen.classList.remove('active');
    loginScreen.style.display = 'none';
  }
  if (grid) grid.style.display = 'none';

  // Strictly show main app container
  if (appEl) {
    appEl.style.display = 'block';
    appEl.classList.add('active');
  }
  loadDashboard();
  buildStages();
}

function showAuthScreen() {
  // Strictly hide main app container
  if (appEl) {
    appEl.classList.remove('active');
    appEl.style.display = 'none';
  }
  // Strictly show login screen
  if (loginScreen) {
    loginScreen.style.display = 'flex';
    loginScreen.classList.add('active');
  }
  if (grid) grid.style.display = 'block';
  if (!lampOn && typeof toggleLamp === 'function') toggleLamp();
}

// ── Service Worker ─────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}

// ── Cursor Grid (login background) ─────────────────────────
(function initGrid() {
  const canvas = document.getElementById('grid-canvas');
  if (!canvas) return;
  const CELL = 70, SIGNAL = '#f3260e', RADIUS = 140, LINE = 1.2;
  let W, H, cols, rows, mouse = { x: -999, y: -999 };
  let cells = [];
  const holdTime = 400, fadeDuration = 800;

  function resize() {
    W = canvas.width = window.innerWidth;
    H = canvas.height = window.innerHeight;
    cols = Math.ceil(W / CELL) + 1;
    rows = Math.ceil(H / CELL) + 1;
    cells = [];
    for (let r = 0; r < rows; r++)
      for (let c = 0; c < cols; c++)
        cells.push({ x: c * CELL, y: r * CELL, o: 0, t: 0, fading: false });
  }

  function smooth(t) { return t * t * (3 - 2 * t); }

  function draw() {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const now = performance.now();

    cells.forEach(cell => {
      const dx = cell.x - mouse.x, dy = cell.y - mouse.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < RADIUS && !cell.fading) {
        cell.o = smooth(1 - dist / RADIUS);
        cell.t = now;
      } else if (now - cell.t > holdTime && cell.o > 0) {
        cell.fading = true;
        cell.o = Math.max(0, cell.o - 16 / fadeDuration);
        if (cell.o <= 0) { cell.o = 0; cell.fading = false; }
      }
      if (cell.o > 0) {
        ctx.strokeStyle = SIGNAL;
        ctx.globalAlpha = cell.o;
        ctx.lineWidth = LINE;
        ctx.strokeRect(cell.x, cell.y, CELL, CELL);
        ctx.globalAlpha = 1;
      }
    });
    requestAnimationFrame(draw);
  }

  window.addEventListener('resize', resize);
  window.addEventListener('mousemove', e => { mouse.x = e.clientX; mouse.y = e.clientY; });
  window.addEventListener('touchmove', e => {
    mouse.x = e.touches[0].clientX;
    mouse.y = e.touches[0].clientY;
  }, { passive: true });

  // Click pulse
  window.addEventListener('click', e => {
    const cx = e.clientX, cy = e.clientY, speed = 600;
    cells.forEach(cell => {
      const dx = cell.x - cx, dy = cell.y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const delay = dist / (RADIUS * 1.5) * speed;
      setTimeout(() => { cell.o = 1; cell.t = performance.now(); }, delay);
    });
  });

  resize(); draw();
})();

// ── Splash → Login flow ────────────────────────────────────
const splash      = document.getElementById('splash');
const loginScreen = document.getElementById('login-screen');
const appEl       = document.getElementById('app');
const grid        = document.getElementById('cursor-grid');

// Shared flag — initialize immediately from localStorage so on refresh we don't flash login screen
let hasActiveSession = hasSavedSupabaseSession();

// Detect OAuth callback URL (hash or code param)
const isOAuthCallback =
  window.location.hash.includes('access_token') ||
  window.location.hash.includes('type=recovery') ||
  new URLSearchParams(window.location.search).has('code');

const splashVideo = document.getElementById('splash-video');
let splashEnded = false;
window.splashVideoEnded = function() {
  if (splashEnded) return;
  splashEnded = true;
  if (!splash) return;
  splash.style.transition = 'opacity .4s ease';
  splash.style.opacity = '0';
  setTimeout(() => {
    splash.style.display = 'none';
    // Re-check session status
    if (!hasActiveSession) hasActiveSession = hasSavedSupabaseSession();

    if (!hasActiveSession && !isOAuthCallback) {
      // User is NOT logged in: show login screen, ensure app is hidden
      if (appEl) {
        appEl.classList.remove('active');
        appEl.style.display = 'none';
      }
      if (loginScreen) {
        loginScreen.style.display = 'flex';
        loginScreen.classList.add('active');
      }
      if (grid) grid.style.display = 'block';
    } else {
      // User IS logged in: show app, ensure login screen is hidden
      if (loginScreen) {
        loginScreen.classList.remove('active');
        loginScreen.style.display = 'none';
      }
      if (grid) grid.style.display = 'none';
      if (appEl) {
        appEl.style.display = 'block';
        appEl.classList.add('active');
      }
    }
  }, 420);
};

if (splashVideo) {
  splashVideo.addEventListener('loadeddata', () => {
    setTimeout(() => {
      document.getElementById('splash-wordmark')?.classList.add('show');
    }, 200);
  });
  const splashDelay = isOAuthCallback ? 800 : 3800;
  setTimeout(() => {
    if (!splashEnded) splashVideoEnded();
  }, splashDelay);
}

// ── Lamp toggle ────────────────────────────────────────────
let lampOn = false;
window.toggleLamp = function() {
  lampOn = !lampOn;
  document.getElementById('lamp-bulb').classList.toggle('on', lampOn);
  document.getElementById('lamp-cone').classList.toggle('on', lampOn);
  document.getElementById('login-panel').classList.toggle('on', lampOn);
  document.getElementById('lamp-hint').style.opacity = lampOn ? '.35' : '1';
  document.getElementById('lamp-hint').textContent = lampOn
    ? 'Pull again to switch off'
    : 'Pull the string to turn on the login form';
};

// ── Auth State & Handlers ──────────────────────────────────
let currentUser = null;
let selectedRole = 'citizen';

window.switchAuthTab = function(tab) {
  const isSignIn = tab === 'signin';
  document.getElementById('tab-btn-signin')?.classList.toggle('active', isSignIn);
  document.getElementById('tab-btn-signup')?.classList.toggle('active', !isSignIn);
  const vSignIn = document.getElementById('auth-view-signin');
  const vSignUp = document.getElementById('auth-view-signup');
  if (vSignIn) vSignIn.style.display = isSignIn ? 'block' : 'none';
  if (vSignUp) vSignUp.style.display = isSignIn ? 'none' : 'block';
};

window.selectRole = function(role) {
  selectedRole = role;
  document.getElementById('role-citizen')?.classList.toggle('active', role === 'citizen');
  document.getElementById('role-officer')?.classList.toggle('active', role === 'officer');
  const badgeWrap = document.getElementById('wrap-badge-field');
  if (badgeWrap) badgeWrap.classList.toggle('show', role === 'officer');
};

window.togglePw = function(fieldId = 'login-pw') {
  const pw = document.getElementById(fieldId);
  if (pw) pw.type = pw.type === 'password' ? 'text' : 'password';
};

function updateUserUI(user, profile) {
  currentUser = (user && profile) ? { user, profile } : null;
  const avatarBadge = document.getElementById('header-avatar-badge');
  const chipName    = document.getElementById('header-user-name');
  const chipRole    = document.getElementById('header-user-role');
  const menuName    = document.getElementById('menu-user-name');
  const menuEmail   = document.getElementById('menu-user-email');
  const authBtnText = document.getElementById('auth-action-text');

  if (user && profile) {
    const displayName = profile.full_name || user.email?.split('@')[0] || 'User';
    const initial = displayName.charAt(0).toUpperCase();
    const roleName = profile.role || 'citizen';

    if (avatarBadge) avatarBadge.textContent = initial;
    if (chipName) chipName.textContent = displayName;
    if (chipRole) {
      chipRole.textContent = roleName;
      chipRole.style.display = 'inline-block';
    }
    if (menuName) menuName.textContent = displayName;
    if (menuEmail) menuEmail.textContent = user.email || '';
    if (authBtnText) authBtnText.textContent = 'Sign Out';
  } else {
    if (avatarBadge) avatarBadge.textContent = '?';
    if (chipName) chipName.textContent = 'Signed out';
    if (chipRole) {
      chipRole.textContent = 'Auth required';
      chipRole.style.display = 'inline-block';
    }
    if (menuName) menuName.textContent = 'Signed out';
    if (menuEmail) menuEmail.textContent = 'Not signed in';
    if (authBtnText) authBtnText.textContent = 'Sign In';
  }
}

window.doSignUp = async function() {
  const nameEl  = document.getElementById('signup-name');
  const emailEl = document.getElementById('signup-email');
  const pwEl    = document.getElementById('signup-pw');
  const badgeEl = document.getElementById('signup-badge');
  const btn     = document.getElementById('btn-submit-signup');

  const name = nameEl?.value?.trim() || '';
  const email = emailEl?.value?.trim() || '';
  const password = pwEl?.value || '';
  const badge = (selectedRole === 'officer' && badgeEl) ? badgeEl.value.trim() : null;

  if (!email || !password) {
    showToast('Please enter an email and password');
    return;
  }
  if (password.length < 6) {
    showToast('Password must be at least 6 characters');
    return;
  }

  const sb = getSB();
  if (!sb) {
    showToast('Connecting to authentication server...');
    return;
  }

  if (btn) { btn.textContent = 'Creating account…'; btn.disabled = true; }

  try {
    const { data, error } = await sb.auth.signUp({
      email: email,
      password: password,
      options: {
        data: {
          full_name: name || email.split('@')[0],
          role: selectedRole,
          badge_number: badge
        }
      }
    });

    if (error) {
      showToast('Registration failed: ' + error.message);
      if (btn) { btn.textContent = 'Create Account'; btn.disabled = false; }
      return;
    }

    showToast('Account created successfully! Welcome to RoadWatch.');

    const user = data.user;
    const profile = {
      id: user?.id,
      email: email,
      full_name: name || email.split('@')[0],
      role: selectedRole,
      badge_number: badge
    };

    updateUserUI(user, profile);

    // Enter app
    enterApp();
  } catch (err) {
    showToast('Error: ' + err.message);
  } finally {
    if (btn) { btn.textContent = 'Create Account'; btn.disabled = false; }
  }
};

window.doLogin = async function() {
  const emailEl = document.getElementById('login-email');
  const pwEl = document.getElementById('login-pw');
  const btn = document.getElementById('btn-submit-signin') || document.querySelector('.btn-signin');

  const email = emailEl?.value?.trim() || '';
  const password = pwEl?.value || '';

  if (!email || !password) {
    showToast('Please enter your email and password');
    return;
  }

  const sb = getSB();
  if (!sb) {
    showToast('Supabase client unavailable. Please check your connection.');
    return;
  }

  if (btn) { btn.textContent = 'Signing in…'; btn.disabled = true; }

  try {
    const { data, error } = await sb.auth.signInWithPassword({
      email: email,
      password: password
    });

    if (error) {
      showToast('Sign-in failed: ' + error.message);
      if (btn) { btn.textContent = 'Sign in'; btn.disabled = false; }
      return;
    }

    // Fetch profile from public.profiles
    let profile = null;
    try {
      const { data: pData } = await sb.from('profiles').select('*').eq('id', data.user.id).single();
      profile = pData;
    } catch (e) {
      console.warn('Profile fetch note:', e);
    }

    if (!profile) {
      profile = {
        id: data.user.id,
        email: data.user.email,
        full_name: data.user.user_metadata?.full_name || data.user.email.split('@')[0],
        role: data.user.user_metadata?.role || 'citizen',
        badge_number: data.user.user_metadata?.badge_number
      };
    }

    updateUserUI(data.user, profile);
    showToast(`Welcome back, ${profile.full_name}!`);

    enterApp();
  } catch (err) {
    showToast('Sign-in error: ' + err.message);
  } finally {
    if (btn) { btn.textContent = 'Sign in'; btn.disabled = false; }
  }
};

window.doGoogleLogin = async function() {
  const sb = getSB();
  if (!sb) {
    showToast('Supabase client unavailable. Please check your connection.');
    return;
  }
  const { error } = await sb.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo: `${window.location.origin}${window.location.pathname}`
    }
  });
  if (error) showToast('Google sign-in failed: ' + error.message);
};

window.doLogout = async function() {
  const sb = getSB();
  if (sb) {
    try { await sb.auth.signOut(); } catch (e) {}
  }
  updateUserUI(null, null);
  closeUserMenu();
  showAuthScreen();
  showToast('Signed out successfully');
};

window.handleAuthAction = function() {
  if (currentUser && currentUser.user) {
    doLogout();
  } else {
    closeUserMenu();
    showAuthScreen();
  }
};

window.toggleUserMenu = function() {
  const dropdown = document.getElementById('user-menu-dropdown');
  if (dropdown) dropdown.classList.toggle('show');
};

window.closeUserMenu = function() {
  const dropdown = document.getElementById('user-menu-dropdown');
  if (dropdown) dropdown.classList.remove('show');
};

// Close dropdown on outside click
document.addEventListener('click', (e) => {
  const wrap = document.getElementById('header-user-wrap');
  if (wrap && !wrap.contains(e.target)) {
    closeUserMenu();
  }
});

// ── Page Navigation ────────────────────────────────────────
const PAGES = ['home', 'demo', 'upload', 'dashboard'];
window.switchPage = function(page) {
  // Standard pages inside #app
  PAGES.forEach(p => {
    document.getElementById('page-' + p)?.classList.toggle('active', p === page);
    document.getElementById('nav-' + p)?.classList.toggle('active', p === page);
  });
  // Trust page lives outside #app pages — toggle separately
  const trustPage = document.getElementById('page-trust');
  const trustNav  = document.getElementById('nav-trust');
  if (trustPage) {
    const isTrust = page === 'trust';
    trustPage.style.display = isTrust ? 'block' : 'none';
    if (trustNav) trustNav.classList.toggle('active', isTrust);
    // Hide app header when on trust (nav stays visible via #app)
    const appHeader = document.getElementById('app-header');
    if (appHeader) appHeader.style.display = isTrust ? 'none' : '';
    if (isTrust) {
      PAGES.forEach(p => document.getElementById('nav-' + p)?.classList.remove('active'));
      renderQueue('trust-queue-list');
    }
  }

  if (page === 'dashboard') refreshDashboard();
  window.scrollTo(0, 0);
};

// ── Demo Section ───────────────────────────────────────────
let currentView = 'raw';
window.setView = function(v) {
  currentView = v;
  document.getElementById('btn-raw')?.classList.toggle('active', v === 'raw');
  document.getElementById('btn-analysed')?.classList.toggle('active', v === 'analysed');
  const img = document.getElementById('demo-img');
  const overlay = document.getElementById('det-overlay');
  if (img) img.src = v === 'raw' ? 'assets/frame-raw.jpg' : 'assets/frame-demo.jpg';
  if (overlay) overlay.style.display = v === 'analysed' ? 'block' : 'none';
};

// ── Vehicle Type ───────────────────────────────────────────
let vehicleType = 'two';
window.selectVehicle = function(t) {
  vehicleType = t;
  document.getElementById('btn-two')?.classList.toggle('selected', t === 'two');
  document.getElementById('btn-four')?.classList.toggle('selected', t === 'four');
  const lbl = document.getElementById('vehicle-label');
  if (lbl) lbl.textContent = t === 'two' ? 'Two-wheeler' : 'Four-wheeler';
};

// ── Drop Zone ──────────────────────────────────────────────
const dropZone = document.getElementById('drop-zone');
if (dropZone) {
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault(); dropZone.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f) startPipeline(f.name, f);
  });
}

window.handleFile = function(e) {
  const f = e.target.files[0];
  if (f) startPipeline(f.name, f);
};

// ── Pipeline Stages ────────────────────────────────────────
const STAGES = [
  { name: 'Frame extraction', meta: 'sample at 0.5s intervals' },
  { name: 'YOLO detection', meta: '4 models: COCO + helmet + plate + class' },
  { name: 'ByteTrack', meta: 'persistent vehicle IDs across frames' },
  { name: 'Rule engine', meta: 'rider count, helmet, phone, wheelie, signal' },
  { name: 'License plate OCR', meta: 'zoom crop → EasyOCR | PaddleOCR | VLM' },
  { name: 'Aggregation', meta: 'clip-level status + severity score' },
  { name: 'VLM tiebreaker', meta: 'Gemini Vision — fires on needs_review only' },
  { name: 'Report', meta: 'report.json + evidence frames' },
];

function buildStages() {
  const list = document.getElementById('stage-list');
  if (!list) return;
  list.innerHTML = STAGES.map((s, i) => `
    <div class="pipeline-stage" id="stage-${i}">
      <div class="stage-dot idle" id="dot-${i}"></div>
      <span class="stage-name" id="sname-${i}">${s.name}</span>
      <span class="stage-meta">${s.meta}</span>
    </div>
  `).join('');
}

function startPipeline(filename, file) {
  const counter = document.getElementById('stage-counter');
  if (counter) counter.textContent = `0 / ${STAGES.length}`;
  STAGES.forEach((_, i) => {
    document.getElementById('dot-' + i).className = 'stage-dot idle';
    document.getElementById('sname-' + i).className = 'stage-name';
  });

  // If we have a real file and backend is available, try uploading it
  if (file) {
    uploadVideoToBackend(file).catch(() => {}); // fire & forget; animate anyway
  }

  const timings = [800, 1400, 1200, 1800, 3000, 1000, 2200, 900];
  let acc = 0;
  STAGES.forEach((_, i) => {
    const start = acc;
    acc += timings[i];
    setTimeout(() => {
      document.getElementById('dot-' + i).className = 'stage-dot running';
      document.getElementById('sname-' + i).className = 'stage-name running';
      if (counter) counter.textContent = `${i + 1} / ${STAGES.length} — ${STAGES[i].name}`;
    }, start);
    setTimeout(() => {
      document.getElementById('dot-' + i).className = 'stage-dot done';
      document.getElementById('sname-' + i).className = 'stage-name done';
    }, acc - 150);
  });

  const totalMs = acc;
  setTimeout(() => {
    if (counter) counter.textContent = '✓ Complete';
    showToast('Analysis submitted — check the Report tab for live results');
    setTimeout(() => switchPage('dashboard'), 1400);
  }, totalMs);
}

// ── Video Upload to Backend ────────────────────────────────
async function uploadVideoToBackend(file) {

  // Get the current Supabase session token (needed by backend auth middleware)
  let authHeader = '';
  try {
    const sb = getSB();
    if (sb) {
      const { data: sessionData } = await sb.auth.getSession();
      if (sessionData?.session?.access_token) {
        authHeader = 'Bearer ' + sessionData.session.access_token;
      }
    }
  } catch (_) {}

  const form = new FormData();
  form.append('file', file);
  form.append('vehicle_type', vehicleType);

  try {
    const headers = {};
    if (authHeader) headers['Authorization'] = authHeader;

    // Fixed: was /upload (404), correct route is /videos/upload
    const resp = await fetch(`${API_BASE}/videos/upload`, {
      method: 'POST',
      body: form,
      headers,
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`Upload failed: ${resp.status} — ${errText}`);
    }
    const data = await resp.json();
    const videoId = data?.data?.video_id || data?.video_id || data?.id;
    console.log('[upload] queued video_id:', videoId);
    showToast('📤 Video queued — AI pipeline will process it shortly');
    return videoId;
  } catch (e) {
    console.warn('[upload] backend unavailable:', e.message);
  }
}

// ══════════════════════════════════════════════════════════
// ── LIVE DASHBOARD — Supabase powered ─────────────────────
// ══════════════════════════════════════════════════════════

let liveViolations = [];
let liveVehicles = [];
let realtimeChannel = null;
let jsonVisible = false;

// Stats counters
const STAT_IDS = {
  total:     'stat-total',
  pending:   'stat-pending',
  confirmed: 'stat-confirmed',
  dismissed: 'stat-dismissed',
};

function updateStats() {
  const total     = liveViolations.length;
  const pending   = liveViolations.filter(v => v.status === 'pending' || v.status === 'needs_review').length;
  const confirmed = liveViolations.filter(v => v.status === 'confirmed').length;
  const dismissed = liveViolations.filter(v => v.status === 'dismissed').length;

  animateCounter('stat-total',     total);
  animateCounter('stat-pending',   pending);
  animateCounter('stat-confirmed', confirmed);
  animateCounter('stat-dismissed', dismissed);

  // Update subtitle
  const sub = document.getElementById('dash-subtitle');
  if (sub) {
    const ago = new Date().toLocaleTimeString();
    sub.textContent = `${total} violation${total !== 1 ? 's' : ''} · last updated ${ago}`;
  }
}

function animateCounter(id, target) {
  const el = document.getElementById(id);
  if (!el) return;
  const start = parseInt(el.textContent) || 0;
  if (start === target) return;
  const steps = 20, dur = 400;
  let i = 0;
  const iv = setInterval(() => {
    i++;
    el.textContent = Math.round(start + (target - start) * (i / steps));
    if (i >= steps) { el.textContent = target; clearInterval(iv); }
  }, dur / steps);
}

async function loadDashboard() {
  const sb = getSB();
  const cardsGrid = document.getElementById('cards-grid');

  if (!sb) {
    // No Supabase available — show offline state
    if (cardsGrid) {
      cardsGrid.innerHTML = renderOfflineState();
    }
    updateStats();
    return;
  }

  // Show loading state
  if (cardsGrid) cardsGrid.innerHTML = renderLoadingState();

  try {
    // Fetch violations (latest 30)
    const { data: violations, error: ve } = await sb
      .from('violations')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(30);

    if (ve) throw ve;
    liveViolations = violations || [];

    // Fetch vehicle_records (latest 30)
    const { data: vehicles, error: vhe } = await sb
      .from('vehicle_records')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(30);

    if (!vhe) liveVehicles = vehicles || [];

    renderCards();
    updateStats();
    subscribeRealtime();

  } catch (err) {
    console.error('[dashboard] Supabase fetch error:', err);
    if (cardsGrid) cardsGrid.innerHTML = renderErrorState(err.message);
    // Still try to show any cached data
    renderCards();
    updateStats();
  }
}

function refreshDashboard() {
  // Called whenever user navigates to dashboard tab
  loadDashboard();
}

function subscribeRealtime() {
  const sb = getSB();
  if (!sb || realtimeChannel) return;

  realtimeChannel = sb
    .channel('violations-live')
    .on('postgres_changes', {
      event: '*',
      schema: 'public',
      table: 'violations'
    }, payload => {
      console.log('[realtime] violation change:', payload.eventType);
      handleRealtimeChange(payload);
    })
    .subscribe(status => {
      const dot = document.getElementById('live-dot');
      if (dot) {
        dot.className = status === 'SUBSCRIBED' ? 'live-dot live-dot--on' : 'live-dot';
        dot.title = status === 'SUBSCRIBED' ? 'Live updates active' : 'Connecting…';
      }
    });
}

function handleRealtimeChange(payload) {
  if (payload.eventType === 'INSERT') {
    liveViolations.unshift(payload.new);
    showToast('🔴 New violation detected — dashboard updated');
  } else if (payload.eventType === 'UPDATE') {
    const idx = liveViolations.findIndex(v => v.id === payload.new.id);
    if (idx >= 0) liveViolations[idx] = payload.new;
  } else if (payload.eventType === 'DELETE') {
    liveViolations = liveViolations.filter(v => v.id !== payload.old.id);
  }
  renderCards();
  updateStats();
}

// ── Card Rendering ─────────────────────────────────────────
function statusBadge(status) {
  const map = {
    'confirmed':    ['badge badge-confirmed', '✓ Confirmed'],
    'needs_review': ['badge badge-review',    '⚠ Needs review'],
    'pending':      ['badge badge-review',    '⏳ Pending'],
    'dismissed':    ['badge badge-dismissed', '— Dismissed'],
    'clean':        ['badge badge-clean',     '✓ Clean'],
  };
  return map[status] || ['badge badge-clean', status || 'Unknown'];
}

function renderCards() {
  const cardsGrid = document.getElementById('cards-grid');
  if (!cardsGrid) return;

  // Merge: violations + vehicle_records
  const sources = liveViolations.length > 0 ? liveViolations : liveVehicles;

  if (sources.length === 0) {
    cardsGrid.innerHTML = renderEmptyState();
    return;
  }

  cardsGrid.innerHTML = sources.slice(0, 20).map((v, idx) => {
    // Support both violations schema and vehicle_records schema
    const plate      = v.plate_number || v.plate || '—';
    const status     = v.status || 'pending';
    const severity   = v.severity_score != null ? (v.severity_score * 100).toFixed(0) + '%' : 'n/a';
    const vtype      = v.vehicle_type || v.class || 'unknown';
    const violations = v.violations || v.violation_types || [];
    const createdAt  = v.created_at ? new Date(v.created_at).toLocaleString() : '—';
    const evidenceUrl = (Array.isArray(v.evidence_urls) && v.evidence_urls[0]) || v.evidence_url || null;
    const allEvidence = Array.isArray(v.evidence_urls) && v.evidence_urls.length > 0
      ? v.evidence_urls
      : (evidenceUrl ? [evidenceUrl] : []);
    const videoId    = v.video_id || v.id;
    const trackId    = v.track_id || '—';
    const [badgeClass, badgeText] = statusBadge(status);

    const violationTags = Array.isArray(violations) && violations.length > 0
      ? violations.map(vt => `<span class="tag tag--signal">${vt}</span>`).join('')
      : `<span class="tag">no violations</span>`;

    const evidenceImagesHtml = allEvidence.length > 0
      ? `<div class="frame-strip" style="display:flex;gap:8px;overflow-x:auto;padding-bottom:6px">
          ${allEvidence.map(imgUrl => `<a href="${imgUrl}" target="_blank" rel="noopener"><img src="${imgUrl}" alt="Evidence frame" style="border-radius:8px;max-height:180px;object-fit:cover;border:1px solid rgba(18,21,28,.12)"></a>`).join('')}
         </div>`
      : '';

    return `
      <article class="card" id="card-${idx}">
        <div class="card-top">
          <div>
            <div class="card-plate">${plate}</div>
            <div class="card-meta">Track ${trackId} · ${vtype}</div>
          </div>
          <span class="${badgeClass}">${badgeText}</span>
        </div>
        <div class="card-tags">${violationTags}</div>
        <div class="card-meta" style="margin-top:6px;font-size:11px;color:var(--navy40)">${createdAt}</div>
        <button class="card-expand-btn" onclick="toggleCard(${idx})" id="expand-btn-${idx}">
          View details
        </button>
        <div id="card-detail-${idx}" style="display:none">
          <div class="card-detail">
            ${evidenceImagesHtml}
            <div class="data-table">
              <div class="data-row"><span>video_id</span><span class="data-val mono" style="font-size:11px">${videoId || '—'}</span></div>
              <div class="data-row"><span>track_id</span><span class="data-val">${trackId}</span></div>
              <div class="data-row"><span>vehicle_type</span><span class="data-val">${vtype}</span></div>
              <div class="data-row"><span>plate</span><span class="data-val">${plate}</span></div>
              <div class="data-row"><span>status</span><span class="data-val">${status}</span></div>
              <div class="data-row"><span>severity</span><span class="data-val">${severity}</span></div>
            </div>
            <p class="reasoning-box">
              ${v.ai_reasoning || v.notes || 'AI analysis recorded. Human review required before any action is taken.'}
            </p>
            <div class="action-row">
              <button class="btn-confirm" onclick="confirmViolation(${idx}, '${v.id}')">Confirm as a person</button>
              <button class="btn-dismiss" onclick="dismissViolation(${idx}, '${v.id}')">Dismiss</button>
            </div>
            <div class="decision-note">This record has advisory status only. No enforcement action is taken automatically.</div>
          </div>
        </div>
      </article>
    `;
  }).join('');
}

// ── Card Actions ───────────────────────────────────────────
window.toggleCard = function(idx) {
  const detail = document.getElementById('card-detail-' + idx);
  const btn = document.getElementById('expand-btn-' + idx);
  if (!detail) return;
  const open = detail.style.display === 'block';
  detail.style.display = open ? 'none' : 'block';
  if (btn) btn.textContent = open ? 'View details' : 'Hide details';
};

window.confirmViolation = async function(idx, id) {
  const sb = getSB();
  if (sb && id && id !== 'undefined') {
    const { error } = await sb
      .from('violations')
      .update({ status: 'confirmed', reviewed_at: new Date().toISOString() })
      .eq('id', id);
    if (error) { showToast('Update failed: ' + error.message); return; }
  }
  // Optimistic update
  if (liveViolations[idx]) liveViolations[idx].status = 'confirmed';
  renderCards();
  updateStats();
  showToast('✓ Confirmed — marked as human-reviewed');
};

window.dismissViolation = async function(idx, id) {
  const sb = getSB();
  if (sb && id && id !== 'undefined') {
    const { error } = await sb
      .from('violations')
      .update({ status: 'dismissed', reviewed_at: new Date().toISOString() })
      .eq('id', id);
    if (error) { showToast('Update failed: ' + error.message); return; }
  }
  if (liveViolations[idx]) liveViolations[idx].status = 'dismissed';
  renderCards();
  updateStats();
  showToast('Dismissed — record archived');
};

// Legacy aliases
window.confirmCard = window.confirmViolation;
window.dismissCard = window.dismissViolation;

// ── State templates ────────────────────────────────────────
function renderLoadingState() {
  return `
    <div style="grid-column:1/-1;text-align:center;padding:48px 16px">
      <div style="width:28px;height:28px;border:2px solid var(--signal);border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 12px"></div>
      <p style="color:var(--navy60);font-size:14px">Loading live data from Supabase…</p>
    </div>`;
}

function renderEmptyState() {
  return `
    <div style="grid-column:1/-1;text-align:center;padding:48px 16px">
      <div style="font-size:36px;margin-bottom:12px">📡</div>
      <p style="font-weight:600;margin-bottom:4px">No violations yet</p>
      <p style="color:var(--navy60);font-size:13px">Upload a dashcam clip and the AI pipeline will populate results here in real time.</p>
    </div>`;
}

function renderOfflineState() {
  return `
    <div style="grid-column:1/-1;text-align:center;padding:48px 16px;background:rgba(255,74,38,.06);border-radius:16px;border:1px solid rgba(255,74,38,.2)">
      <div style="font-size:36px;margin-bottom:12px">⚡</div>
      <p style="font-weight:600;margin-bottom:4px">Supabase not connected</p>
      <p style="color:var(--navy60);font-size:13px">Add the Supabase CDN script to index.html or check your network.</p>
    </div>`;
}

function renderErrorState(msg) {
  return `
    <div style="grid-column:1/-1;text-align:center;padding:32px 16px;background:rgba(255,74,38,.06);border-radius:16px;border:1px solid rgba(255,74,38,.2)">
      <p style="font-weight:600;margin-bottom:4px;color:var(--signal)">Fetch error</p>
      <p style="color:var(--navy60);font-size:12px;font-family:monospace">${msg}</p>
      <button onclick="loadDashboard()" style="margin-top:12px;padding:8px 16px;background:var(--signal);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px">Retry</button>
    </div>`;
}

// ── JSON Toggle ────────────────────────────────────────────
window.toggleJson = function() {
  jsonVisible = !jsonVisible;
  const pre = document.getElementById('json-pre');
  const btn = document.getElementById('json-btn');
  if (jsonVisible) {
    const payload = { violations: liveViolations.slice(0, 5), vehicles: liveVehicles.slice(0, 3) };
    if (pre) pre.textContent = JSON.stringify(payload, null, 2);
  }
  if (pre) pre.style.display = jsonVisible ? 'block' : 'none';
  if (btn) btn.textContent = jsonVisible ? 'Hide raw JSON' : 'Show raw JSON';
};

// ── Toast ──────────────────────────────────────────────────
let toastTimer;
window.showToast = function(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3000);
};

// ── Officer Review Queue ────────────────────────────────────
const QUEUE_DATA = [
  { plate: 'MH01DP4248', detail: 'Plate disagreement · escalated to vision model', wait: '4 min', color: '#B8860B' },
  { plate: 'Track 47 · no plate', detail: 'Helmet flag contested by second opinion', wait: '11 min', color: '#B8860B' },
  { plate: 'MH43AD9202', detail: 'Single valid read at 85% · needs eyes on frame', wait: '26 min', color: '#FF4A26' }
];

window.renderQueue = function(targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  el.innerHTML = QUEUE_DATA.map(q => `
    <div class="queue-row">
      <span class="queue-dot" style="background:${q.color}"></span>
      <div style="min-width:0;flex:1">
        <div class="queue-plate">${q.plate}</div>
        <div class="queue-detail">${q.detail}</div>
      </div>
      <span class="queue-wait">${q.wait}</span>
    </div>
  `).join('');
};

// Render queues on both pages at startup
renderQueue('review-queue-list');

// ── Session Init + Auth State Listener (registered ONCE) ────
let _authListenerRegistered = false;
let _enterAppCalled = false;

function _safeEnterApp(user, profile) {
  if (_enterAppCalled) return; // prevent double-entry
  _enterAppCalled = true;
  hasActiveSession = true;
  updateUserUI(user, profile);
  enterApp();
}

async function initAuthSession() {
  const sb = getSB();
  if (!sb) return;

  // Register listener only once
  if (!_authListenerRegistered) {
    _authListenerRegistered = true;
    sb.auth.onAuthStateChange(async (event, session) => {
      if ((event === 'SIGNED_IN' || event === 'INITIAL_SESSION') && session?.user) {
        hasActiveSession = true;
        // Fetch profile
        let profile = null;
        try {
          const { data: pData } = await sb.from('profiles').select('*').eq('id', session.user.id).single();
          profile = pData;
        } catch (pe) {}
        if (!profile) {
          profile = {
            id: session.user.id,
            email: session.user.email,
            full_name: session.user.user_metadata?.full_name ||
                       session.user.user_metadata?.name ||
                       session.user.email.split('@')[0],
            role: session.user.user_metadata?.role || 'citizen',
            badge_number: session.user.user_metadata?.badge_number || null,
          };
        }
        _safeEnterApp(session.user, profile);
        if (event === 'SIGNED_IN' && !session.user.user_metadata?.fromInit) {
          showToast(`Welcome, ${profile.full_name || session.user.email}!`);
        }
        // Clean up OAuth hash
        if (window.location.hash.includes('access_token')) {
          window.history.replaceState(null, '', window.location.pathname + window.location.search);
        }
      } else if (event === 'SIGNED_OUT') {
        hasActiveSession = false;
        _enterAppCalled = false;
        updateUserUI(null, null);
        showAuthScreen();
      }
    });
  }

  // Explicit getSession() check for returning users (session may exist before listener fires)
  try {
    const { data: { session } } = await sb.auth.getSession();
    if (session?.user) {
      hasActiveSession = true;
      let profile = null;
      try {
        const { data: pData } = await sb.from('profiles').select('*').eq('id', session.user.id).single();
        profile = pData;
      } catch (pe) {}
      _safeEnterApp(session.user, profile || {
        id: session.user.id,
        email: session.user.email,
        full_name: session.user.user_metadata?.full_name ||
                   session.user.user_metadata?.name ||
                   session.user.email.split('@')[0],
        role: session.user.user_metadata?.role || 'citizen',
      });
    }
  } catch (err) {
    console.log('Session check note:', err);
  }
}

// Single init call — DOMContentLoaded is sufficient, no setTimeout needed
window.addEventListener('DOMContentLoaded', initAuthSession);

