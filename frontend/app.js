/* app.js — RoadWatch.AI — v4 portals (static, no build step). Implements docs/PRODUCT_FLOW_DECISIONS.md §2 against backend/API.md v3. */
'use strict';

// ── Supabase config (anon key is safe to expose — RLS protects data) ──
const SUPABASE_URL = 'https://fbjjoktuzirhpqqpzfbo.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZiampva3R1emlyaHBxcXB6ZmJvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU0MjA1MzcsImV4cCI6MjEwMDk5NjUzN30.sp9Kgpt7alImqzhzWkWo1Gx4FTzut0Fzm9IPu8fX0po';

// ── Backend API URL — local dev vs deployed Render ──
const RENDER_URL = 'https://raksharider.onrender.com';
const IS_LOCAL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const API_BASE = IS_LOCAL ? 'http://localhost:8000' : RENDER_URL;

// Dev hook: ?preview=citizen|officer|admin|landing renders the shell with fixture data. Localhost only.
const PREVIEW_ROLE = (() => {
  if (!IS_LOCAL) return null;
  const p = new URLSearchParams(window.location.search).get('preview');
  return ['citizen', 'officer', 'admin', 'landing'].includes(p) ? p : null;
})();
const PREVIEW_USER = PREVIEW_ROLE && PREVIEW_ROLE !== 'landing' ? PREVIEW_ROLE : null;

let _sb = null;
function getSB() {
  if (_sb) return _sb;
  if (window.supabase) { _sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY); return _sb; }
  return null;
}
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

// ══════════════════════════════════════════════════════════
// ── Static policy copies (mirror pipeline/contract.py VIOLATION_POLICY) ──
// ══════════════════════════════════════════════════════════
const VIOLATION_POLICY = {
  wheelie:          { label: 'Wheelie (stunt riding)',   tier: 'top',    two_wheeler: true,  four_wheeler: false, review_only: true },
  phone_usage:      { label: 'Phone use while driving',  tier: 'top',    two_wheeler: true,  four_wheeler: true,  review_only: false },
  wrong_way:        { label: 'Wrong side / wrong way',   tier: 'top',    two_wheeler: true,  four_wheeler: true,  review_only: true },
  signal_violation: { label: 'Red-light violation',      tier: 'top',    two_wheeler: true,  four_wheeler: true,  review_only: true },
  no_helmet:        { label: 'Riding without helmet',    tier: 'middle', two_wheeler: true,  four_wheeler: false, review_only: false },
  triple_riding:    { label: 'Triple riding',            tier: 'middle', two_wheeler: true,  four_wheeler: false, review_only: false },
  lane_cutting:     { label: 'Lane cutting',             tier: 'middle', two_wheeler: true,  four_wheeler: true,  review_only: true },
  erratic_driving:  { label: 'Erratic driving',          tier: 'middle', two_wheeler: true,  four_wheeler: true,  review_only: false },
  missing_plate:    { label: 'Missing / obscured plate', tier: 'minor',  two_wheeler: true,  four_wheeler: true,  review_only: true },
  no_seatbelt:      { label: 'No seatbelt',              tier: 'minor',  two_wheeler: false, four_wheeler: true,  review_only: true },
};
const TIER_LABEL = { top: 'Top', middle: 'Middle', minor: 'Minor' };
const REJECTION_FALLBACK = { helmet_worn: 'Helmet actually worn', wrong_vehicle: 'Wrong vehicle identified', plate_misread: 'Plate misread', footage_unclear: 'Footage too unclear', not_a_violation: 'Not a violation', duplicate_case: 'Duplicate of another case', other: 'Other' };
const PLATE_RE = /^([A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}|[0-9]{2}BH[0-9]{4}[A-Z]{1,2})$/;
const USER_STATUS = { uploading: 'Uploading', queued: 'Queued', analysing: 'Analysing', awaiting_review: 'Awaiting review', decided: 'Decided', withdrawn: 'Withdrawn', could_not_process: 'Could not process' };
const RAW_TO_USER = { uploading: 'uploading', unprocessed: 'queued', processing: 'analysing', processed: 'awaiting_review', failed: 'could_not_process', withdrawn: 'withdrawn' };

// ══════════════════════════════════════════════════════════
// ── Helpers ───────────────────────────────────────────────
// ══════════════════════════════════════════════════════════
const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ESC_MAP[c]); }
// Evidence and video only via https signed URLs. Preview (localhost) may use local sample assets.
function safeUrl(u) {
  if (typeof u !== 'string') return null;
  if (/^https:\/\//i.test(u)) return u;
  if (PREVIEW_USER && /^assets\/[\w.-]+$/.test(u)) return u;
  return null;
}
function short(id) { return String(id || '').slice(0, 8); }
function ago(iso) {
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  if (s < 7 * 86400) return Math.round(s / 86400) + ' d ago';
  return new Date(iso).toLocaleDateString();
}
function dur(sec) {
  sec = Number(sec) || 0;
  if (sec < 90) return Math.round(sec) + ' s';
  if (sec < 5400) return Math.round(sec / 60) + ' min';
  if (sec < 172800) return (sec / 3600).toFixed(1) + ' h';
  return Math.round(sec / 86400) + ' d';
}
function fmtDate(iso) { return iso ? new Date(iso).toLocaleString() : '—'; }
function pct(x) { return (x == null || isNaN(x)) ? '—' : Math.round(Number(x) * (Number(x) <= 1 ? 100 : 1)) + '%'; }
function vtLabel(t) { return ({ two_wheeler: 'Two-wheeler', four_wheeler: 'Four-wheeler', two: 'Two-wheeler', four: 'Four-wheeler' })[t] || (t ? String(t).replace(/_/g, ' ') : 'unknown'); }
function roleLabel(r) { return ({ officer: 'Reviewer', admin: 'Admin', citizen: 'User' })[r] || 'User'; }
function humanType(t) { return String(t || '').replace(/_/g, ' '); }
function vioLabel(v) { return v ? (VIOLATION_POLICY[v]?.label || humanType(v)) : 'Not specified'; }
function userStatus(v) { return v.user_status || RAW_TO_USER[v.status] || v.status || 'queued'; }
function caseNo(id) { return 'Case ' + String(id || '').slice(0, 8).toUpperCase(); }
function isEmail(s) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s); }

const CHIP = {
  // user-facing submission words
  uploading: ['chip-amber', 'Uploading'], queued: ['chip-amber', 'Queued'], analysing: ['chip-amber chip-live', 'Analysing'],
  awaiting_review: ['chip-amber', 'Awaiting review'], decided: ['chip-green', 'Decided'], withdrawn: ['chip-grey', 'Withdrawn'], could_not_process: ['chip-red', 'Could not process'],
  // raw video words (admin)
  unprocessed: ['chip-amber', 'Queued'], processing: ['chip-amber chip-live', 'Processing'], processed: ['chip-green', 'Processed'], failed: ['chip-red', 'Failed'],
  // case status
  pending_review: ['chip-amber', 'Pending review'], in_review: ['chip-amber chip-live', 'In review'], second_opinion: ['chip-amber', 'Second opinion'], finalized: ['chip-green', 'Finalized'], reopened: ['chip-red', 'Reopened'],
  // finding decisions + AI results
  pending: ['chip-grey', 'Pending'], confirmed: ['chip-green', 'Confirmed'], rejected: ['chip-grey', 'Rejected'], inconclusive: ['chip-amber', 'Inconclusive'], unverifiable: ['chip-grey', 'Unverifiable'],
  needs_review: ['chip-amber', 'Needs review'], observed_absent: ['chip-grey', 'Not observed'], unobservable: ['chip-grey', 'Unobservable'], not_evaluated: ['chip-grey', 'Not evaluated'],
  supported: ['chip-green', 'Supported'], not_supported: ['chip-red', 'Not supported'], not_declared: ['chip-grey', 'Not declared'],
  clean: ['chip-grey', 'Clean'], observed: ['chip-amber', 'Observed'], watch: ['chip-amber', 'Watch'], escalated: ['chip-red', 'Escalated'],
  pending_approval: ['chip-amber', 'Pending approval'], approved: ['chip-red', 'Approved'], dismissed: ['chip-grey', 'Dismissed'],
  resolved: ['chip-green', 'Resolved'], provisional: ['chip-amber', 'Provisional'], ambiguous: ['chip-amber', 'Ambiguous'], conflict: ['chip-red', 'Conflict'],
  A: ['chip-green', 'Tier A'], B: ['chip-amber', 'Tier B'], C: ['chip-grey', 'Tier C'],
};
function chip(status) {
  const [cls, label] = CHIP[status] || ['chip-grey', humanType(status) || 'unknown'];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
}
function tile(num, label, sub, color) {
  return `<div class="tile"><div class="tile-num" style="${color ? 'color:' + color : ''}">${esc(num)}</div><div class="tile-label">${esc(label)}</div>${sub ? `<div class="tile-sub">${esc(sub)}</div>` : ''}</div>`;
}
function table(cols, rows) {
  return `<div class="table-wrap"><table class="admin-table"><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}
function empty(title, body) { return `<div class="empty"><b>${esc(title)}</b>${esc(body || '')}</div>`; }
function failBox(e, retryAction) {
  return `<div class="empty"><b>${e.preview ? 'Preview mode' : 'Unavailable'}</b>${esc(e.preview ? 'No backend data in preview.' : e.message)}${retryAction ? `<div style="margin-top:12px"><button class="btn btn-sm" type="button" data-action="${esc(retryAction)}">Retry</button></div>` : ''}</div>`;
}
function list(arr) { return Array.isArray(arr) ? arr : (arr?.items || arr?.rows || arr?.cases || arr?.videos || arr?.users || []); }

// All backend calls go through here: attaches the Supabase access token, throws on non-2xx with detail.error, unwraps {success,data}.
async function api(path, { method = 'GET', body } = {}) {
  if (PREVIEW_USER) return previewApi(path, method);
  const headers = {};
  try {
    const { data } = await getSB().auth.getSession();
    if (data?.session?.access_token) headers.Authorization = 'Bearer ' + data.session.access_token;
  } catch (_) {}
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(API_BASE + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (_) { const e = new Error('Backend unreachable'); e.status = 0; throw e; }
  let json = null;
  try { json = await res.json(); } catch (_) {}
  if (!res.ok) {
    const d = json?.detail;
    const msg = (d && typeof d === 'object' && d.error) || (typeof d === 'string' && d) || json?.error || `Request failed (${res.status})`;
    const e = new Error(msg); e.status = res.status; throw e;
  }
  return (json && typeof json === 'object' && 'data' in json) ? json.data : json;
}
// Supabase read wrapper: never throws, empty on error / preview.
async function q(builder) {
  if (PREVIEW_USER) return { data: [], count: 0 };
  try {
    const { data, error, count } = await builder;
    if (error) { console.warn('[supabase]', error.message); return { data: [], count: 0, error }; }
    return { data: data || [], count: count || 0 };
  } catch (e) { console.warn('[supabase]', e.message); return { data: [], count: 0, error: e }; }
}

// ── Preview fixtures (localhost only; fake plates only) ──
function previewApi(path, method) {
  if (method !== 'GET') { const e = new Error('Preview mode — nothing is saved'); e.preview = true; e.status = 0; throw e; }
  const now = Date.now(), iso = m => new Date(now - m * 60000).toISOString();
  const me = '00000000-0000-0000-0000-000000000000';
  const findings = [
    { id: 'f1a2b3c4d5e6f7a8b9c0', violation: 'no_helmet', ai_result: 'confirmed', tier: 'A', confidence: 0.83, agreement: 0.9, evidence_frames: 4, evaluable_frames: 5, reasoning: 'No-helmet box on the rider head in 4 of 5 keyframes; helmet detector never fired.', decision: 'pending', rejection_reason: null, note: null },
    { id: 'f2b3c4d5e6f7a8b9c0d1', violation: 'triple_riding', ai_result: 'needs_review', tier: 'B', confidence: 0.58, agreement: 0.6, evidence_frames: 2, evaluable_frames: 5, reasoning: 'Three person boxes overlap the motorcycle in 2 keyframes; occlusion by the car on the left in the rest.', decision: 'pending', rejection_reason: null, note: null },
  ];
  const evidence = [
    { id: 'e1', finding_id: findings[0].id, frame_index: 372, timestamp: 15.5, blob_path: 'v/r/7/372.jpg', url: 'assets/sample-t15.jpg' },
    { id: 'e2', finding_id: findings[0].id, frame_index: 384, timestamp: 16.0, blob_path: 'v/r/7/384.jpg', url: 'assets/sample-t16.jpg' },
    { id: 'e3', finding_id: findings[0].id, frame_index: 396, timestamp: 16.5, blob_path: 'v/r/7/396.jpg', url: 'assets/sample-t17.jpg' },
    { id: 'e4', finding_id: findings[1].id, frame_index: 384, timestamp: 16.0, blob_path: 'v/r/7/384b.jpg', url: 'assets/sample-t16.jpg' },
  ];
  const caseRow = (id, extra) => ({ id, video_id: 'a1b2c3d4-0000-4000-8000-000000000001', run_id: 'run-1', track_id: 7, is_subject: true, lane: 'normal', status: 'pending_review', priority: 2, identity_status: 'resolved', vehicle_type: 'two_wheeler', plate: { ai: 'MH12AB1234', confidence: 0.81, claimed: 'MH12AB1234', corrected: null }, findings_count: 2, claimed_by: null, claimed_at: null, allegation_answer: 'supported', created_at: iso(90), ...extra });
  const cases = [
    caseRow('c1000000-0000-4000-8000-000000000001', {}),
    caseRow('c2000000-0000-4000-8000-000000000002', { track_id: 3, is_subject: false, priority: 3, identity_status: 'provisional', vehicle_type: 'four_wheeler', plate: { ai: null, confidence: 0, claimed: null, corrected: null }, findings_count: 1, allegation_answer: null, created_at: iso(30) }),
    caseRow('c3000000-0000-4000-8000-000000000003', { lane: 'not_supported', allegation_answer: 'not_supported', identity_status: 'conflict', plate: { ai: 'DL01CD5678', confidence: 0.66, claimed: 'DL01CD5673', corrected: null }, findings_count: 1, created_at: iso(200) }),
    caseRow('c4000000-0000-4000-8000-000000000004', { status: 'in_review', claimed_by: me, claimed_at: iso(60 * 26), idle_hours: 26 }),
    caseRow('c5000000-0000-4000-8000-000000000005', { status: 'finalized', claimed_by: me, finalized_by: me, finalized_at: iso(60 * 5), created_at: iso(60 * 30) }),
  ];
  const videos = [
    { id: 'a1b2c3d4-0000-4000-8000-000000000001', original_name: 'dashcam_0917_0812.mp4', status: 'processed', user_status: 'awaiting_review', vehicle_type: 'two_wheeler', declared_violation: 'no_helmet', claimed_plate: 'MH12AB1234', note: 'Rider overtook me from the left without a helmet, two pillions on the bike.', priority: 2, allegation_answer: 'supported', uploaded_at: iso(95), uploaded_by: me, recording_at: iso(150), location_text: 'Near Dadar TT, Mumbai' },
    { id: 'a1b2c3d4-0000-4000-8000-000000000002', original_name: 'IMG_4471.mov', status: 'processing', user_status: 'analysing', vehicle_type: 'four_wheeler', declared_violation: 'phone_usage', priority: 3, uploaded_at: iso(12), uploaded_by: me, claimed_at: iso(4), attempts: 1 },
    { id: 'a1b2c3d4-0000-4000-8000-000000000003', original_name: 'clip_signal.mp4', status: 'unprocessed', user_status: 'queued', vehicle_type: 'two_wheeler', declared_violation: 'wheelie', priority: 3, uploaded_at: iso(3), uploaded_by: me },
    { id: 'a1b2c3d4-0000-4000-8000-000000000004', original_name: 'evening_ride.mp4', status: 'processed', user_status: 'decided', vehicle_type: 'two_wheeler', declared_violation: 'triple_riding', priority: 2, allegation_answer: 'not_supported', uploaded_at: iso(60 * 50), uploaded_by: me },
    { id: 'a1b2c3d4-0000-4000-8000-000000000005', original_name: 'broken.mp4', status: 'failed', user_status: 'could_not_process', vehicle_type: 'four_wheeler', declared_violation: null, priority: 0, error_reason: 'Could not decode the video stream', error_category: 'download', uploaded_at: iso(60 * 9), uploaded_by: me, attempts: 3 },
  ];
  const users = [
    { id: me, full_name: 'Preview Admin', email: 'preview-admin@localhost', role: 'admin', created_at: iso(60 * 24 * 40) },
    { id: 'u2', full_name: 'Asha Verma', email: 'asha@example.com', role: 'citizen', requested_role: 'officer', role_requested_at: iso(60 * 5), badge_number: 'MH-TP-402', created_at: iso(60 * 24 * 2) },
    { id: 'u3', full_name: 'Rohit Singh', email: 'rohit@example.com', role: 'officer', badge_number: 'MH-TP-118', verified_via: 'Traffic HQ roster', created_at: iso(60 * 24 * 20) },
    { id: 'u4', full_name: 'Nina D', email: 'nina@example.com', role: 'citizen', created_at: iso(60 * 24 * 7) },
  ];
  const m = (re) => path.match(re);
  if (path === '/auth/me') return { user_id: me };
  if (m(/^\/videos(\?|$)/)) return videos;
  if (m(/^\/videos\/([^/?]+)$/)) {
    const v = videos.find(x => x.id === decodeURIComponent(m(/^\/videos\/([^/?]+)$/)[1])) || videos[0];
    const decided = v.user_status === 'decided';
    return { video: v, detection_video_url: null /* preview: no signed video */, cases: [
      { id: cases[0].id, track_id: 7, is_subject: true, status: decided ? 'finalized' : 'pending_review', identity_status: 'resolved', vehicle_type: 'two_wheeler', plate: v.claimed_plate || 'MH12AB1234',
        findings: findings.map(f => ({ ...f, decision: decided ? (f.violation === 'no_helmet' ? 'confirmed' : 'rejected') : 'pending', rejection_reason: decided && f.violation !== 'no_helmet' ? 'footage_unclear' : null })), evidence: evidence.slice(0, 3) },
      { id: cases[1].id, track_id: 3, is_subject: false, status: 'pending_review', identity_status: 'provisional', vehicle_type: 'four_wheeler', plate: null, findings: [{ id: 'f3', violation: 'phone_usage', ai_result: 'needs_review', tier: 'B', decision: 'pending' }], evidence: [] },
    ] };
  }
  if (path === '/cases/mine') return cases.filter(c => c.status === 'in_review');
  if (m(/^\/cases\/([^/?]+)$/)) {
    const c = cases.find(x => x.id === m(/^\/cases\/([^/?]+)$/)[1]) || cases[0];
    return { ...c, status: c.status === 'pending_review' ? 'in_review' : c.status, claimed_by: me, claimed_at: c.claimed_at || iso(5), cycle: 1,
      uploader_claim: { declared_violation: 'no_helmet', claimed_plate: 'MH12AB1234', note: 'Rider overtook me from the left without a helmet, two pillions on the bike.' },
      ai: {
        track: { track_id: 7, vehicle_class: 'motorcycle', class_confidence: 0.91, first_seen: 14.2, last_seen: 17.9, frames_observed: 88, identity_status: 'resolved' },
        plate_observations: [{ text: 'MH12AB1234', engine: 'easyocr', confidence: 0.81, frame_index: 384 }, { text: 'MH12AB1234', engine: 'paddleocr', confidence: 0.77, frame_index: 396 }],
        resolved_plate: { text: 'MH12AB1234', confidence: 0.81 },
        model_versions: { detector: 'yolo11m-1280', helmet: 'helmet-v3', plate: 'plate-v2', tracker: 'botsort-reid' }, pipeline_version: '3.0.0',
        vlm_calls: [{ track_id: 7, scope: 'triple_riding', model: 'gemini-2.5-flash', output: 'cannot determine rider count', error: null }],
        findings,
      },
      evidence, corrections: [], decisions: [], plate_of_record: 'MH12AB1234',
      // mirrors GET /cases/{id}: {confirmed, observed} with no plate/status keys, and a pending withdrawal on the claimed case
      withdrawal_request: c.status === 'in_review' ? { id: 'w1', status: 'pending', reason: 'I filmed the wrong bike', created_at: iso(20) } : null,
      plate_history: c.status === 'finalized' ? { confirmed: [], observed: [{ layer: 'observed', violation: 'no_helmet', case_id: c.id, created_at: iso(60 * 24 * 9) }] } : null };
  }
  if (m(/^\/cases(\?|$)/)) {
    const p = new URLSearchParams(path.split('?')[1] || '');
    return cases.filter(c => (!p.get('lane') || c.lane === p.get('lane')) && (!p.get('status') || c.status === p.get('status')));
  }
  if (path === '/rejection-reasons') return Object.entries(REJECTION_FALLBACK).map(([code, label], i) => ({ code, label, sort: i }));
  if (m(/^\/plates\//)) return { plate: decodeURIComponent(path.split('/')[2]), status: 'watch', confirmed: [{ violation: 'no_helmet', case_id: cases[4].id, video_id: videos[0].id, created_at: iso(60 * 5) }, { violation: 'triple_riding', case_id: cases[4].id, video_id: videos[0].id, created_at: iso(60 * 24 * 3) }], observed: [{ violation: 'phone_usage', case_id: cases[0].id, video_id: videos[0].id, created_at: iso(60 * 24 * 12) }], escalations: [] };
  if (path.startsWith('/admin/live')) return { users_online: 4, uploads_today: 11, queue: { by_priority: { 3: 1, 2: 2, 1: 0, 0: 1 }, depth: 4 }, processing: [{ video_id: videos[1].id, elapsed_s: 214, attempts: 1 }], worker_last_seen: iso(1), failed_today: 1, review: { backlog: 3, oldest_waiting_s: 12100, claimed: 1, reviewers_active: [{ id: 'u3', name: 'Rohit Singh', open_claims: 1 }] } };
  if (path.startsWith('/admin/queue')) return { counts: { uploading: 0, unprocessed: 1, processing: 1, processed: 2, failed: 1 }, paused: false, worker_last_seen: iso(1), oldest_unprocessed_at: iso(3), processing: [videos[1]], failed: [videos[4]] };
  if (path.startsWith('/admin/quality')) return { rejections_by_reason: { helmet_worn: 6, footage_unclear: 4, plate_misread: 3, wrong_vehicle: 1, not_a_violation: 2 }, agreement_by_violation: { no_helmet: { confirmed: 31, rejected: 6, inconclusive: 2 }, triple_riding: { confirmed: 9, rejected: 3, inconclusive: 4 }, phone_usage: { confirmed: 4, rejected: 5, inconclusive: 1 } }, plate_misread_rate: 0.07, not_supported_rate: 0.18, avg_review_seconds: 412 };
  if (path.startsWith('/admin/escalations')) return [{ id: 'esc1', plate: 'DL01CD5678', status: 'pending_approval', threshold_hit: 'three confirmed findings', created_at: iso(40), package: { plate: 'DL01CD5678', cases: [{ case_id: cases[4].id, violation: 'no_helmet', decision: 'confirmed', reviewer_badge: 'MH-TP-118' }] } }];
  if (path.startsWith('/admin/users')) return users;
  if (path.startsWith('/admin/audit')) return [{ created_at: iso(20), actor_role: 'admin', actor_id: me, action: 'case.reopen', entity: 'case', entity_id: cases[4].id, reason: 'Uploader supplied a clearer clip' }, { created_at: iso(300), actor_role: 'officer', actor_id: 'u3', action: 'case.finalize', entity: 'case', entity_id: cases[4].id, reason: '' }];
  if (path.startsWith('/admin/settings')) return { max_uploads_per_day: 10, escalation_threshold_any: 3, escalation_threshold_top: 1, retention_days: 90, worker_paused: false };
  if (path.startsWith('/evidence/sign')) return { url: 'assets/sample-t16.jpg' };
  return {};
}

// ══════════════════════════════════════════════════════════
// ── Screens: splash → landing → login → app ───────────────
// ══════════════════════════════════════════════════════════
const splash      = document.getElementById('splash');
const landingEl   = document.getElementById('landing');
const loginScreen = document.getElementById('login-screen');
const appEl       = document.getElementById('app');
const grid        = document.getElementById('cursor-grid');

function hideAll() {
  if (loginScreen) { loginScreen.classList.remove('active'); loginScreen.style.display = 'none'; }
  if (grid) grid.style.display = 'none';
  if (landingEl) landingEl.classList.remove('active');
  if (appEl) { appEl.classList.remove('active'); appEl.style.display = 'none'; }
}
function enterApp() {
  splashEnded = true;
  if (splash) { splash.style.display = 'none'; splash.style.opacity = '0'; }
  hideAll();
  if (appEl) { appEl.style.display = ''; appEl.classList.add('active'); }
  initApp();
}
function showLanding() {
  teardownApp();
  hideAll();
  if (landingEl) landingEl.classList.add('active');
  window.scrollTo(0, 0);
}
function showAuthScreen() { showLanding(); }
// Opened from a landing button: lamp turns on by itself.
window.openLogin = function(tab) {
  hideAll();
  if (loginScreen) { loginScreen.style.display = 'flex'; loginScreen.classList.add('active'); }
  if (grid) grid.style.display = 'block';
  switchAuthTab(tab === 'signup' ? 'signup' : 'signin');
  if (!lampOn) toggleLamp();
};
window.closeLogin = function() { showLanding(); };

// ── Cursor Grid (login background) ─────────────────────────
(function initGrid() {
  const canvas = document.getElementById('grid-canvas');
  if (!canvas) return;
  const CELL = 70, SIGNAL = '#f3260e', RADIUS = 140, LINE = 1.2;
  let W, H, cols, rows, mouse = { x: -999, y: -999 };
  let cells = [];
  const holdTime = 400, fadeDuration = 800;
  function resize() {
    W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight;
    cols = Math.ceil(W / CELL) + 1; rows = Math.ceil(H / CELL) + 1;
    cells = [];
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) cells.push({ x: c * CELL, y: r * CELL, o: 0, t: 0, fading: false });
  }
  function smooth(t) { return t * t * (3 - 2 * t); }
  function draw() {
    if (grid && grid.style.display !== 'none') {
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, W, H);
      const now = performance.now();
      cells.forEach(cell => {
        const dx = cell.x - mouse.x, dy = cell.y - mouse.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < RADIUS && !cell.fading) { cell.o = smooth(1 - dist / RADIUS); cell.t = now; }
        else if (now - cell.t > holdTime && cell.o > 0) { cell.fading = true; cell.o = Math.max(0, cell.o - 16 / fadeDuration); if (cell.o <= 0) { cell.o = 0; cell.fading = false; } }
        if (cell.o > 0) { ctx.strokeStyle = SIGNAL; ctx.globalAlpha = cell.o; ctx.lineWidth = LINE; ctx.strokeRect(cell.x, cell.y, CELL, CELL); ctx.globalAlpha = 1; }
      });
    }
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize', resize);
  window.addEventListener('mousemove', e => { mouse.x = e.clientX; mouse.y = e.clientY; });
  window.addEventListener('touchmove', e => { mouse.x = e.touches[0].clientX; mouse.y = e.touches[0].clientY; }, { passive: true });
  window.addEventListener('click', e => {
    if (!loginScreen?.classList.contains('active')) return;
    const cx = e.clientX, cy = e.clientY, speed = 600;
    cells.forEach(cell => {
      const dx = cell.x - cx, dy = cell.y - cy, dist = Math.sqrt(dx * dx + dy * dy);
      setTimeout(() => { cell.o = 1; cell.t = performance.now(); }, dist / (RADIUS * 1.5) * speed);
    });
  });
  resize(); draw();
})();

// ── Splash flow ────────────────────────────────────────────
let hasActiveSession = hasSavedSupabaseSession() || !!PREVIEW_USER;
const isOAuthCallback = window.location.hash.includes('access_token') || window.location.hash.includes('type=recovery') || new URLSearchParams(window.location.search).has('code');
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
    if (!hasActiveSession) hasActiveSession = hasSavedSupabaseSession();
    if (!hasActiveSession && !isOAuthCallback) showLanding();
    else if (currentUser) enterApp();
    else { hideAll(); if (appEl) { appEl.style.display = ''; appEl.classList.add('active'); } }
  }, 420);
};
if (splashVideo) {
  splashVideo.addEventListener('loadeddata', () => setTimeout(() => document.getElementById('splash-wordmark')?.classList.add('show'), 200));
  setTimeout(() => { if (!splashEnded) splashVideoEnded(); }, (isOAuthCallback || PREVIEW_ROLE) ? 800 : 3800);
}

// ── Lamp toggle ────────────────────────────────────────────
let lampOn = false;
window.toggleLamp = function() {
  lampOn = !lampOn;
  document.getElementById('lamp-bulb').classList.toggle('on', lampOn);
  document.getElementById('lamp-cone').classList.toggle('on', lampOn);
  document.getElementById('login-panel').classList.toggle('on', lampOn);
  document.getElementById('lamp-hint').style.opacity = lampOn ? '.35' : '1';
  document.getElementById('lamp-hint').textContent = lampOn ? 'Pull again to switch off' : 'Pull the string to turn on the login form';
};

// ══════════════════════════════════════════════════════════
// ── Auth State & Handlers ─────────────────────────────────
// ══════════════════════════════════════════════════════════
let currentUser = null;
window.switchAuthTab = function(tab) {
  const isSignIn = tab === 'signin';
  document.getElementById('tab-btn-signin')?.classList.toggle('active', isSignIn);
  document.getElementById('tab-btn-signup')?.classList.toggle('active', !isSignIn);
  const vSignIn = document.getElementById('auth-view-signin'), vSignUp = document.getElementById('auth-view-signup');
  if (vSignIn) vSignIn.style.display = isSignIn ? 'block' : 'none';
  if (vSignUp) vSignUp.style.display = isSignIn ? 'none' : 'block';
};
window.toggleOfficerRequest = function(cb) { document.getElementById('wrap-badge-field')?.classList.toggle('show', !!cb.checked); };
window.togglePw = function(fieldId = 'login-pw') { const pw = document.getElementById(fieldId); if (pw) pw.type = pw.type === 'password' ? 'text' : 'password'; };

// Role always comes from public.profiles.role — never from user_metadata.
function profileFromUser(user, pData) {
  if (pData) return pData;
  return { id: user.id, email: user.email, full_name: user.user_metadata?.full_name || user.user_metadata?.name || (user.email || '').split('@')[0], role: 'citizen' };
}
async function fetchProfile(sb, user) {
  let pData = null;
  try { const { data } = await sb.from('profiles').select('*').eq('id', user.id).single(); pData = data; } catch (e) { console.warn('Profile fetch note:', e); }
  return profileFromUser(user, pData);
}
function updateUserUI(user, profile) {
  currentUser = (user && profile) ? { user, profile } : null;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  if (user && profile) {
    const displayName = profile.full_name || user.email?.split('@')[0] || 'User';
    set('header-avatar-badge', displayName.charAt(0).toUpperCase()); set('header-user-name', displayName); set('header-user-role', roleLabel(profile.role));
    set('menu-user-name', displayName); set('menu-user-email', user.email || ''); set('auth-action-text', 'Sign Out');
  } else {
    set('header-avatar-badge', '?'); set('header-user-name', 'Signed out'); set('header-user-role', '—');
    set('menu-user-name', 'Signed out'); set('menu-user-email', 'Not signed in'); set('auth-action-text', 'Sign In');
  }
}

window.doSignUp = async function() {
  const name = document.getElementById('signup-name')?.value?.trim() || '';
  const email = document.getElementById('signup-email')?.value?.trim() || '';
  const password = document.getElementById('signup-pw')?.value || '';
  const wantsOfficer = !!document.getElementById('signup-officer')?.checked;
  const badge = wantsOfficer ? (document.getElementById('signup-badge')?.value.trim() || '') : null;
  const btn = document.getElementById('btn-submit-signup');
  if (!email || !password) { showToast('Please enter an email and password'); return; }
  if (!isEmail(email)) { showToast('Please enter a valid email address'); return; }
  if (password.length < 6) { showToast('Password must be at least 6 characters'); return; }
  if (wantsOfficer && !badge) { showToast('Badge number is required to request reviewer access'); return; }
  const sb = getSB();
  if (!sb) { showToast('Connecting to authentication server...'); return; }
  if (btn) { btn.textContent = 'Creating account…'; btn.disabled = true; }
  try {
    const { data, error } = await sb.auth.signUp({ email, password, options: { data: { full_name: name || email.split('@')[0], role: wantsOfficer ? 'officer' : 'citizen', badge_number: badge || null } } });
    if (error) { showToast('Registration failed: ' + error.message); return; }
    if (!data.session) { showToast('Account created — check your inbox to confirm your e-mail, then sign in.'); switchAuthTab('signin'); return; }
    showToast(wantsOfficer ? 'Account created. Reviewer access requested — an admin will approve it.' : 'Account created successfully! Welcome to RoadWatch.');
  } catch (err) { showToast('Error: ' + err.message); }
  finally { if (btn) { btn.textContent = 'Create Account'; btn.disabled = false; } }
};
window.doLogin = async function() {
  const email = document.getElementById('login-email')?.value?.trim() || '';
  const password = document.getElementById('login-pw')?.value || '';
  const btn = document.getElementById('btn-submit-signin');
  if (!email || !password) { showToast('Please enter your email and password'); return; }
  const sb = getSB();
  if (!sb) { showToast('Supabase client unavailable. Please check your connection.'); return; }
  if (btn) { btn.textContent = 'Signing in…'; btn.disabled = true; }
  try {
    const { data, error } = await sb.auth.signInWithPassword({ email, password });
    if (error) { showToast('Sign-in failed: ' + error.message); return; }
    const profile = await fetchProfile(sb, data.user);
    showToast(`Welcome back, ${profile.full_name || email}!`);
    _safeEnterApp(data.user, profile);
  } catch (err) { showToast('Sign-in error: ' + err.message); }
  finally { if (btn) { btn.textContent = 'Sign in'; btn.disabled = false; } }
};
window.doGoogleLogin = async function() {
  const sb = getSB();
  if (!sb) { showToast('Supabase client unavailable. Please check your connection.'); return; }
  const { error } = await sb.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: `${window.location.origin}${window.location.pathname}` } });
  if (error) showToast('Google sign-in failed: ' + error.message);
};
window.doLogout = async function() {
  const sb = getSB();
  if (sb) { try { await sb.auth.signOut(); } catch (e) {} }
  _enterAppCalled = false; hasActiveSession = false;
  updateUserUI(null, null); closeUserMenu();
  window.location.hash = '';
  showLanding();
  showToast('Signed out successfully');
};
window.handleAuthAction = function() { if (currentUser?.user) doLogout(); else { closeUserMenu(); openLogin('signin'); } };
window.toggleUserMenu = function() { document.getElementById('user-menu-dropdown')?.classList.toggle('show'); };
window.closeUserMenu = function() { document.getElementById('user-menu-dropdown')?.classList.remove('show'); };
document.addEventListener('click', e => { const wrap = document.getElementById('header-user-wrap'); if (wrap && !wrap.contains(e.target)) closeUserMenu(); });

// ══════════════════════════════════════════════════════════
// ── Navigation + hash routing ─────────────────────────────
// ══════════════════════════════════════════════════════════
const I = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">${d}</svg>`;
const ICONS = {
  home: I('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/>'),
  upload: I('<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/>'),
  submissions: I('<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 9h20M7 5v14M17 5v14"/>'),
  alerts: I('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>'),
  help: I('<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 015.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'),
  queue: I('<path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z"/><path d="M9 12l2 2 4-4"/>'),
  mycases: I('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
  completed: I('<path d="M20 6L9 17l-5-5"/>'),
  stats: I('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'),
  live: I('<circle cx="12" cy="12" r="3"/><path d="M4.9 4.9a10 10 0 000 14.2M19.1 4.9a10 10 0 010 14.2M7.8 7.8a6 6 0 000 8.4M16.2 7.8a6 6 0 010 8.4"/>'),
  processing: I('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>'),
  cases: I('<path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h5"/>'),
  quality: I('<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>'),
  escalations: I('<path d="M12 3l9 18H3z"/><path d="M12 10v4M12 17h.01"/>'),
  plates: I('<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6 12h2M10 12h4M16 12h2"/>'),
  users: I('<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8"/>'),
  audit: I('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h6"/>'),
  settings: I('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/>'),
};
const TITLES = {
  home: 'Home', upload: 'Upload a clip', submissions: 'My submissions', submission: 'Submission', alerts: 'Alerts', help: 'Help',
  queue: 'Queue', mycases: 'My cases', case: 'Case', completed: 'Completed', stats: 'My stats',
  live: 'Live board', processing: 'Processing', cases: 'Cases', quality: 'Model quality', escalations: 'Escalations', plates: 'Plate history', users: 'Users', audit: 'Audit', settings: 'Settings',
};
const NAV_SHORT = { submissions: 'Mine', mycases: 'Cases', completed: 'Done', processing: 'Ops', escalations: 'Escal.', quality: 'Quality' };
const NAV_BY_ROLE = {
  citizen: ['home', 'upload', 'submissions', 'alerts', 'help'],
  officer: ['queue', 'mycases', 'completed', 'stats', 'alerts', 'help'],
  admin:   ['live', 'processing', 'cases', 'quality', 'escalations', 'plates', 'users', 'audit', 'settings', 'alerts'],
};
const CITIZEN_PAGES = ['home', 'upload', 'submissions', 'submission', 'alerts', 'help'];
const OFFICER_PAGES = ['queue', 'mycases', 'case', 'completed', 'stats'];
const ADMIN_PAGES = ['live', 'processing', 'cases', 'quality', 'escalations', 'plates', 'users', 'audit', 'settings'];
const ALIAS = { clips: 'submissions', clip: 'submission', review: 'queue', admin: 'live' };

function role() { return currentUser?.profile?.role || 'citizen'; }
function uid() { return currentUser?.user?.id || null; }
function isReviewer() { return role() === 'officer' || role() === 'admin'; }
function isAdmin() { return role() === 'admin'; }
function homePage() { return isAdmin() ? 'live' : isReviewer() ? 'queue' : 'home'; }
function canView(page) {
  if (ADMIN_PAGES.includes(page)) return isAdmin();
  if (OFFICER_PAGES.includes(page)) return isReviewer();
  return CITIZEN_PAGES.includes(page);
}
function buildNav() {
  const items = NAV_BY_ROLE[role()] || NAV_BY_ROLE.citizen;
  const btn = (cls, p, label) => `<button class="${cls}" type="button" data-action="nav" data-page="${p}" data-nav="${p}">${ICONS[p] || ''}<span>${esc(label)}</span>${cls === 'nav-item' ? '<div class="nav-dot"></div>' : ''}${p === 'alerts' ? '<span class="nav-badge" data-unread hidden>0</span>' : ''}</button>`;
  const side = document.getElementById('side-nav'), bottom = document.getElementById('bottom-nav');
  if (side) side.innerHTML = items.map(p => btn('side-item', p, TITLES[p])).join('');
  if (bottom) bottom.innerHTML = items.map(p => btn('nav-item', p, NAV_SHORT[p] || TITLES[p])).join('');
}

let currentPage = null, currentId = null, pageTimer = null;
const PAGE_FN = {};
function route() {
  if (!appEl?.classList.contains('active') || !currentUser) return;
  const raw = decodeURIComponent(window.location.hash.replace(/^#\/?/, ''));
  if (raw.includes('access_token') || raw.includes('type=recovery')) return;
  let [page, id] = raw.split('/');
  page = ALIAS[page] || page;
  if (!page || page === 'overview' || !canView(page)) { window.location.replace('#' + homePage()); return; }
  clearInterval(pageTimer); pageTimer = null;
  document.querySelectorAll('[data-nav]').forEach(b => b.classList.toggle('active', b.dataset.nav === page || (page === 'submission' && b.dataset.nav === 'submissions') || (page === 'case' && b.dataset.nav === (isAdmin() ? 'cases' : 'mycases'))));
  const title = document.getElementById('header-title');
  if (title) title.textContent = TITLES[page] || page;
  window.scrollTo(0, 0);
  closeUserMenu(); closeDrawer(); closeModal();
  currentPage = page; currentId = id || null;
  const el = document.getElementById('page');
  if (el) { el.classList.remove('active'); void el.offsetWidth; el.classList.add('active'); }
  (PAGE_FN[page] || (() => setPage('')))(id);
}
window.addEventListener('hashchange', route);
function setPage(html) { const el = document.getElementById('page'); if (el) el.innerHTML = html; }
function head(title, sub, actions) {
  return `<div class="page-head"><div><h2 class="section-title">${esc(title)}</h2>${sub ? `<p class="section-sub">${esc(sub)}</p>` : ''}</div>${actions ? `<div class="page-actions">${actions}</div>` : ''}</div>`;
}

// ── Init / teardown once the user is in ────────────────────
let appInited = false, presenceTimer = null;
function initApp() {
  if (appInited) return;
  appInited = true;
  buildNav();
  subscribeRealtime();
  loadNotifications();
  // presence heartbeat: every 60 s while logged in
  const beat = () => api('/auth/presence', { method: 'POST' }).catch(() => {});
  beat(); presenceTimer = setInterval(beat, 60000);
  route();
}
function teardownApp() {
  appInited = false; currentPage = null;
  clearInterval(presenceTimer); clearInterval(pageTimer);
  const sb = getSB();
  channels.forEach(ch => { try { sb?.removeChannel(ch); } catch (_) {} });
  channels = [];
  state.notifications = []; state.videos = []; state.kase = null;
  renderNotifs();
}

// ══════════════════════════════════════════════════════════
// ── State + realtime ──────────────────────────────────────
// ══════════════════════════════════════════════════════════
const state = { notifications: [], alertsFilter: 'all', videos: [], queueTab: 'normal', kase: null, reasons: null, admin: null, evidenceSel: {} };
let channels = [], reloadTimer = null;
function subscribeRealtime() {
  const sb = getSB();
  if (!sb || PREVIEW_USER || channels.length || !uid()) { setLive(PREVIEW_USER ? 'Preview' : null); return; }
  channels.push(
    sb.channel('notifications-' + uid())
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'notifications', filter: `recipient_id=eq.${uid()}` }, p => onNotification(p.new))
      .subscribe(),
    sb.channel('videos-live')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'videos' }, () => reloadIf(['home', 'submissions', 'submission', 'live', 'processing']))
      .subscribe(status => setLive(status === 'SUBSCRIBED' ? 'Live' : 'Connecting')),
    sb.channel('cases-live')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'cases' }, () => reloadIf(['queue', 'mycases', 'completed', 'cases', 'live', 'submission']))
      .subscribe(),
  );
}
function setLive(label) {
  const el = document.getElementById('live-chip');
  if (!el) return;
  el.textContent = label || 'Offline';
  el.className = 'chip ' + (label === 'Live' ? 'chip-green chip-live' : 'chip-grey');
}
// Debounced re-render of the current page when a realtime row changes (never touches a case being edited).
function reloadIf(pages) {
  if (!pages.includes(currentPage) || currentPage === 'case') return;
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(() => { if (pages.includes(currentPage)) (PAGE_FN[currentPage] || (() => {}))(currentId, true); }, 900);
}

// ══════════════════════════════════════════════════════════
// ── Notifications (bell, drawer, alerts page, desktop) ────
// ══════════════════════════════════════════════════════════
async function loadNotifications() {
  const sb = getSB(); if (!sb) return;
  const { data } = await q(sb.from('notifications').select('*').order('created_at', { ascending: false }).limit(100));
  state.notifications = data;
  renderNotifs();
}
function onNotification(n) {
  if (!n || state.notifications.some(x => x.id === n.id)) return;
  state.notifications.unshift(n);
  renderNotifs();
  showToast(n.title || 'New alert');
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification(n.title || 'RoadWatch.AI', { body: n.body || '', icon: 'icons/icon-192.png', tag: n.id }); } catch (_) {}
  }
  reloadIf(['home', 'live', 'queue', 'mycases']);
}
function unreadCount() { return state.notifications.filter(n => !n.read_at).length; }
function notifItem(n) {
  return `<button class="notif ${n.read_at ? '' : 'unread'}" type="button" data-action="notif-open" data-id="${esc(n.id)}">
      <span class="notif-bar ${esc(n.severity || 'info')}"></span>
      <span class="notif-main"><div class="notif-title">${esc(n.title)}</div>${n.body ? `<div class="notif-body">${esc(n.body)}</div>` : ''}<div class="notif-time">${esc(ago(n.created_at))} · ${esc(humanType(n.kind))}</div></span>
      ${n.read_at ? '' : '<span class="notif-dot" title="Unread"></span>'}</button>`;
}
function renderNotifList(el, items, emptyMsg) {
  if (!el) return;
  el.innerHTML = items.length ? items.map(notifItem).join('') : `<div class="empty" style="border:0;background:none"><b>Nothing here</b>${esc(emptyMsg)}</div>`;
}
function renderNotifs() {
  const n = unreadCount();
  const bell = document.getElementById('bell-count');
  if (bell) { bell.textContent = n > 99 ? '99+' : String(n); bell.hidden = n === 0; }
  document.querySelectorAll('[data-unread]').forEach(b => { b.textContent = n > 99 ? '99+' : String(n); b.hidden = n === 0; });
  renderNotifList(document.getElementById('drawer-list'), state.notifications.slice(0, 30), 'New alerts will appear here in real time.');
  if (currentPage === 'alerts') renderAlertsList();
}
// Where a notification leads, by what it carries and who is looking.
function notifTarget(n) {
  const k = String(n.kind || '');
  if (isAdmin()) {
    if (/escalat/.test(k)) return '#escalations';
    if (/reviewer_request|role/.test(k)) return '#users';
    if (/worker|process|fail/.test(k)) return '#processing';
    if (/withdraw/.test(k) && n.case_id) return '#case/' + n.case_id;
  }
  if (n.case_id && isReviewer()) return '#case/' + n.case_id;
  if (n.video_id) return '#submission/' + n.video_id;
  if (isReviewer() && /case|claim|second/.test(k)) return isAdmin() ? '#cases' : '#queue';
  return null;
}
async function markRead(ids) {
  ids = ids.filter(id => { const n = state.notifications.find(x => x.id === id); return n && !n.read_at; });
  if (!ids.length) return;
  const now = new Date().toISOString();
  ids.forEach(id => { const n = state.notifications.find(x => x.id === id); if (n) n.read_at = now; });
  renderNotifs();
  const sb = getSB();
  if (sb && !PREVIEW_USER) {
    const { error } = await sb.from('notifications').update({ read_at: now }).in('id', ids);
    if (error) showToast('Could not mark as read: ' + error.message);
  }
}
function openDrawer() { document.getElementById('notif-drawer')?.classList.add('open'); document.getElementById('drawer-backdrop')?.classList.add('open'); }
function closeDrawer() { document.getElementById('notif-drawer')?.classList.remove('open'); document.getElementById('drawer-backdrop')?.classList.remove('open'); }

PAGE_FN.alerts = function() {
  setPage(head('Alerts', 'Processing results, reviewer decisions and role changes land here.', `
    <div class="seg" id="alerts-filter"><button type="button" data-action="alerts-filter" data-filter="all">All</button><button type="button" data-action="alerts-filter" data-filter="unread">Unread</button></div>
    <button class="btn btn-sm" type="button" data-action="mark-all-read">Mark all read</button>
    <button class="btn btn-sm" type="button" id="btn-desktop-alerts" data-action="enable-desktop">Enable desktop alerts</button>`)
    + `<div class="panel" style="padding:0;overflow:hidden"><div id="alerts-list"></div></div>`);
  renderAlertsList();
};
function renderAlertsList() {
  document.querySelectorAll('#alerts-filter button').forEach(b => b.classList.toggle('active', b.dataset.filter === state.alertsFilter));
  const items = state.alertsFilter === 'unread' ? state.notifications.filter(n => !n.read_at) : state.notifications;
  renderNotifList(document.getElementById('alerts-list'), items, state.alertsFilter === 'unread' ? 'You are all caught up.' : 'You will be told here when something changes on your submissions.');
  const btn = document.getElementById('btn-desktop-alerts');
  if (btn) {
    const perm = ('Notification' in window) ? Notification.permission : 'unsupported';
    btn.textContent = perm === 'granted' ? 'Desktop alerts on' : perm === 'denied' ? 'Desktop alerts blocked' : 'Enable desktop alerts';
    btn.disabled = perm !== 'default';
  }
}

// ══════════════════════════════════════════════════════════
// ── USER portal ───────────────────────────────────────────
// ══════════════════════════════════════════════════════════
async function loadVideos() {
  try { state.videos = list(await api('/videos?limit=100')); return null; }
  catch (e) { return e; }
}
function subName(v) { return v.original_name || v.filename || ('Clip ' + short(v.id)); }
function subMeta(v) {
  const parts = [ago(v.uploaded_at || v.created_at), vtLabel(v.vehicle_type)];
  if (v.declared_violation) parts.push(vioLabel(v.declared_violation));
  return parts.join(' · ');
}
function subRow(v) {
  const s = userStatus(v);
  return `<button class="row-card" type="button" data-action="open-sub" data-id="${esc(v.id)}">
      <div class="row-main"><div class="row-title">${esc(subName(v))}</div><div class="row-meta">${esc(subMeta(v))}</div></div>
      <div class="row-right">${chip(s)}</div></button>`;
}

PAGE_FN.home = async function(_, silent) {
  if (!silent) setPage(head(`Hello, ${currentUser?.profile?.full_name || 'there'}`, 'Your submissions at a glance.', `<span class="chip chip-grey" id="live-chip">Connecting</span><button class="btn btn-signal" type="button" data-action="nav" data-page="upload">Upload a clip</button>`)
    + `<div class="tiles" id="home-tiles"></div><div class="panel"><div class="panel-title">Recent submissions</div><div id="home-recent"><div class="spinner"></div></div></div>
       <p class="decision-note" style="max-width:70ch">Everything the AI reports is <b>provisional</b> until a human reviewer decides. A decision on a finding is <b>verified</b>; nothing here is a fine or a penalty — RoadWatch never issues one.</p>`);
  const err = await loadVideos();
  if (currentPage !== 'home') return;
  const c = k => state.videos.filter(v => userStatus(v) === k).length;
  const tiles = document.getElementById('home-tiles');
  if (tiles) tiles.innerHTML = err ? failBox(err) : tile(state.videos.length, 'Uploaded') + tile(c('queued') + c('analysing') + c('uploading'), 'Analysing', 'queued or running', c('analysing') ? 'var(--amber)' : null)
    + tile(c('awaiting_review'), 'Awaiting review', 'a human is next', c('awaiting_review') ? 'var(--amber)' : null) + tile(c('decided'), 'Decided', 'verified by a reviewer', 'var(--green)');
  const rec = document.getElementById('home-recent');
  if (rec) rec.innerHTML = err ? '' : (state.videos.length ? state.videos.slice(0, 5).map(subRow).join('') : empty('No submissions yet', 'Upload your first clip and follow it here.'));
  setLive(PREVIEW_USER ? 'Preview' : channels.length ? 'Live' : null);
};

PAGE_FN.submissions = async function(_, silent) {
  if (!silent) setPage(head('My submissions', 'Every clip you sent, in plain words.', `<button class="btn btn-sm" type="button" data-action="nav" data-page="upload">Upload a clip</button>`) + `<div id="sub-list"><div class="spinner"></div></div>`);
  const err = await loadVideos();
  if (currentPage !== 'submissions') return;
  const el = document.getElementById('sub-list');
  if (el) el.innerHTML = err ? failBox(err, 'refresh') : (state.videos.length ? state.videos.map(subRow).join('') : empty('No submissions yet', 'Your uploads will show up here with their status.'));
};

// ── Upload form ──
let vehicleType = 'two_wheeler', pendingFile = null;
window.selectVehicle = function(t) {
  vehicleType = t === 'four' || t === 'four_wheeler' ? 'four_wheeler' : 'two_wheeler';
  document.getElementById('btn-two')?.classList.toggle('selected', vehicleType === 'two_wheeler');
  document.getElementById('btn-four')?.classList.toggle('selected', vehicleType === 'four_wheeler');
  const lbl = document.getElementById('vehicle-label'); if (lbl) lbl.textContent = vtLabel(vehicleType);
  const sel = document.getElementById('up-violation');
  if (sel) {
    const keep = sel.value;
    sel.innerHTML = '<option value="">Not sure / something else</option>' + Object.entries(VIOLATION_POLICY).filter(([, p]) => p[vehicleType]).map(([k, p]) => `<option value="${k}">${esc(p.label)} · ${TIER_LABEL[p.tier]}${p.review_only ? ' · manual check' : ''}</option>`).join('');
    sel.value = keep; if (sel.value !== keep) sel.value = '';
  }
};
window.handleFile = function(e) { const f = e.target.files[0]; if (f) setFile(f); };
function setFile(f) {
  if (!f.type.startsWith('video/')) { showToast('Please choose a video file'); return; }
  if (f.size > MAX_BYTES) { showToast('File too large — the limit is 200 MB'); return; }
  pendingFile = f;
  const t = document.getElementById('drop-title'); if (t) t.textContent = f.name;
  const s = document.getElementById('drop-sub'); if (s) s.textContent = `${(f.size / 1048576).toFixed(1)} MB · tap to change`;
}
PAGE_FN.upload = function() {
  pendingFile = null;
  setPage(head('Send us a clip', 'Tell us what you saw. The AI checks it first; a human always decides.') + `
    <form id="upload-form" class="panel" novalidate>
      <div class="field"><label>Vehicle you are reporting</label>
        <div class="vehicle-type-row" style="margin-bottom:0">
          <button class="vehicle-btn selected" id="btn-two" onclick="selectVehicle('two')" type="button">Two-wheeler</button>
          <button class="vehicle-btn" id="btn-four" onclick="selectVehicle('four')" type="button">Four-wheeler</button>
        </div></div>
      <label class="drop-zone" id="drop-zone" style="padding:18px">
        <input type="file" id="file-input" accept="video/*" onchange="handleFile(event)">
        <div class="drop-icon-row"><svg viewBox="0 0 24 24" fill="none" stroke="#FF4A26" stroke-width="1.7" style="width:28px;height:28px;flex:none"><path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>
          <div><div class="drop-title" id="drop-title">Drop a video here, or tap to choose</div><div class="drop-sub" id="drop-sub">MP4 / MOV / AVI · up to 200 MB · <span id="vehicle-label">Two-wheeler</span> selected</div></div></div>
      </label>
      <div class="field" style="margin-top:14px"><label for="up-violation">What you saw</label><select class="select" id="up-violation" name="declared_violation"></select>
        <div class="hint">Sets how urgently the clip is checked. Items marked "manual check" are always judged by a person.</div></div>
      <div class="field"><label for="up-note">What happened (2–3 lines)</label><textarea class="textarea" id="up-note" name="note" minlength="10" maxlength="400" required placeholder="e.g. The rider overtook me from the left at the signal with no helmet and two pillions."></textarea>
        <div class="hint"><span id="note-count">0</span>/400 · at least 10 characters</div></div>
      <div class="field"><label for="up-plate">Plate, if you could read it (optional)</label><input class="input mono" id="up-plate" name="claimed_plate" maxlength="12" autocapitalize="characters" placeholder="MH12AB1234">
        <div class="hint">Indian format: state code + district + series + 4 digits (e.g. MH12AB1234, or 22BH1234AA). A typo never convicts the wrong owner — the AI checks it against the footage.</div></div>
      <div class="two-col">
        <div class="field"><label for="up-when">Recorded on (optional)</label><input class="input" id="up-when" name="recording_at" type="datetime-local"></div>
        <div class="field"><label for="up-where">Rough location (optional)</label><input class="input" id="up-where" name="location_text" maxlength="200" placeholder="e.g. Near Dadar TT, Mumbai"></div>
      </div>
      <label class="check-row"><input type="checkbox" id="up-consent" name="consent"><span>I confirm this footage is genuine, recorded by me or with permission, and I am allowed to submit it.</span></label>
      <div class="action-row"><button class="btn btn-signal" type="submit" id="btn-upload" style="flex:1">Submit clip</button></div>
    </form>
    <div id="upload-cards"></div>
    <p class="decision-note" style="max-width:70ch">Your file goes straight to private storage over a short-lived link. 10 uploads per day, 200 MB per file. Other vehicles in your clip are analysed too; their plates are never shown to you.</p>`);
  selectVehicle('two');
  const dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag-over'); const f = e.dataTransfer.files[0]; if (f) setFile(f); });
  document.getElementById('up-note').addEventListener('input', e => { document.getElementById('note-count').textContent = e.target.value.length; });
  document.getElementById('upload-form').addEventListener('submit', e => { e.preventDefault(); submitUpload(e.target); });
};
const MAX_BYTES = 200 * 1024 * 1024;
let uploadSeq = 0;
function putWithProgress(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.setRequestHeader('Content-Type', file.type || 'video/mp4');
    if (/blob\.core\.windows\.net/i.test(url)) xhr.setRequestHeader('x-ms-blob-type', 'BlockBlob'); else xhr.setRequestHeader('x-upsert', 'false');
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100)); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300) ? resolve() : reject(new Error(`Storage rejected the file (${xhr.status})`));
    xhr.onerror = () => reject(new Error('Network error while uploading'));
    xhr.send(file);
  });
}
async function submitUpload(f) {
  const file = pendingFile;
  const note = f.note.value.trim();
  const plate = f.claimed_plate.value.toUpperCase().replace(/[\s-]+/g, '');
  const declared = f.declared_violation.value || null;
  if (!file) { showToast('Choose a video first'); return; }
  if (file.size > MAX_BYTES) { showToast('File too large — the limit is 200 MB'); return; }
  if (note.length < 10 || note.length > 400) { showToast('The note must be between 10 and 400 characters'); f.note.focus(); return; }
  if (plate && !PLATE_RE.test(plate)) { showToast('That does not look like an Indian plate (e.g. MH12AB1234). Leave it blank if unsure.'); f.claimed_plate.focus(); return; }
  if (declared && !VIOLATION_POLICY[declared]?.[vehicleType]) { showToast('That violation does not apply to this vehicle type'); return; }
  if (!f.consent.checked) { showToast('Please confirm the footage is genuine and yours to submit'); f.consent.focus(); return; }
  const body = { filename: file.name, content_type: file.type || 'video/mp4', size_bytes: file.size, vehicle_type: vehicleType, declared_violation: declared, note, consent: true };
  if (plate) body.claimed_plate = plate;
  if (f.recording_at.value) body.recording_at = new Date(f.recording_at.value).toISOString();
  if (f.location_text.value.trim()) body.location_text = f.location_text.value.trim();

  const btn = document.getElementById('btn-upload'); btn.disabled = true;
  const key = 'up-' + (++uploadSeq);
  document.getElementById('upload-cards').insertAdjacentHTML('afterbegin', `
    <div class="panel" id="${key}"><div class="panel-title"><span style="min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(file.name)}</span><span data-upload-chip>${chip('uploading')}</span></div>
      <div class="row-meta" data-upload-msg>Preparing upload… · ${esc(vtLabel(vehicleType))} · ${(file.size / 1048576).toFixed(1)} MB</div>
      <div class="progress"><i data-upload-bar></i></div></div>`);
  const card = document.getElementById(key), msg = card.querySelector('[data-upload-msg]'), bar = card.querySelector('[data-upload-bar]'), chipEl = card.querySelector('[data-upload-chip]');
  try {
    const init = await api('/videos/upload/init', { method: 'POST', body });
    let url = init.upload_url;
    if (url && !/^https?:\/\//.test(url)) url = SUPABASE_URL + '/storage/v1' + (url.startsWith('/') ? '' : '/') + url;
    msg.textContent = 'Uploading… 0%';
    await putWithProgress(url, file, p => { bar.style.width = p + '%'; msg.textContent = `Uploading… ${p}%`; });
    bar.style.width = '100%'; msg.textContent = 'Finalising…';
    const row = await api('/videos/upload/complete', { method: 'POST', body: { video_id: init.video_id } });
    const video = (row && row.id) ? row : { id: init.video_id, status: 'unprocessed', user_status: 'queued', uploaded_at: new Date().toISOString(), vehicle_type: vehicleType, original_name: file.name, declared_violation: declared };
    card.outerHTML = subRow(video);
    f.reset(); pendingFile = null; selectVehicle('two');
    document.getElementById('drop-title').textContent = 'Drop a video here, or tap to choose';
    document.getElementById('drop-sub').innerHTML = 'MP4 / MOV / AVI · up to 200 MB · <span id="vehicle-label">Two-wheeler</span> selected';
    document.getElementById('note-count').textContent = '0';
    showToast('Clip queued — you will get an alert when it has been analysed');
  } catch (e) {
    const text = e.status === 429 ? 'Daily upload limit reached (10 per day) — try again tomorrow.'
      : e.status === 413 ? 'File too large — the limit is 200 MB.'
      : e.status === 422 ? 'The form was not accepted: ' + e.message
      : e.status === 401 || e.status === 403 ? 'Please sign in again to upload.'
      : e.preview ? 'Preview mode — uploads are disabled.' : e.message;
    chipEl.innerHTML = chip('could_not_process'); msg.textContent = text; msg.style.color = '#c43818'; bar.style.background = '#c43818';
    showToast(text);
  } finally { btn.disabled = false; }
}

// ── Submission detail ──
function aiAnswerSentence(v) {
  const what = vioLabel(v.declared_violation).toLowerCase();
  switch (v.allegation_answer) {
    case 'supported': return `The AI found signs of ${what} on the vehicle you described. A reviewer will confirm or reject each finding.`;
    case 'not_supported': return `The AI could not find ${what} on the vehicle you described. A reviewer will check the clip by hand before anything is closed.`;
    case 'unobservable': return `The footage does not show enough to judge ${what}. A reviewer will look at it by hand.`;
    case 'not_declared': return 'You did not name a violation, so the AI reported everything it could see. A reviewer decides what counts.';
    default: return null;
  }
}
// AI output is never a decision — label it as such, never reuse the reviewer-decision chips.
const AI_LABEL = { confirmed: 'AI: detected', needs_review: 'AI: unsure', observed_absent: 'AI: not seen', unobservable: 'AI: cannot tell', not_evaluated: 'AI: manual check' };
function findingRow(fd, decided) {
  const dec = fd.decision && fd.decision !== 'pending' ? fd.decision : null;
  const aiRes = fd.ai_result || fd.result;
  return `<div class="verdict"><div class="verdict-head"><span class="verdict-type">${esc(vioLabel(fd.violation))}</span><span><span class="chip chip-grey">${esc(AI_LABEL[aiRes] || 'AI: ' + humanType(aiRes))}</span>${dec ? ' ' + chip(dec) : ''}</span></div>
    ${dec === 'rejected' && fd.rejection_reason ? `<div class="verdict-reason">Reviewer's reason: ${esc(reasonLabel(fd.rejection_reason))}${fd.note_public ? ' — ' + esc(fd.note_public) : ''}</div>` : ''}
    ${!dec && !decided ? '<div class="verdict-reason">Waiting for a reviewer.</div>' : ''}</div>`;
}
function reasonLabel(code) { return state.reasons?.find(r => r.code === code)?.label || REJECTION_FALLBACK[code] || humanType(code); }
PAGE_FN.submission = async function(id) {
  setPage(`<button class="back-link" type="button" data-action="nav" data-page="submissions">← All submissions</button><div class="spinner"></div>`);
  let d = null, err = null;
  try { d = await api('/videos/' + encodeURIComponent(id)); } catch (e) { err = e; }
  if (currentPage !== 'submission' || currentId !== id) return;
  if (!d || !d.video) { setPage(`<button class="back-link" type="button" data-action="nav" data-page="submissions">← All submissions</button>${failBox(err || new Error('Could not load the submission'), 'refresh')}`); return; }
  const v = d.video, cases = list(d.cases), st = userStatus(v);
  const subject = cases.find(c => c.is_subject);
  const others = cases.filter(c => !c.is_subject);
  const decided = st === 'decided';
  const inReview = cases.some(c => c.status === 'in_review');
  const answer = aiAnswerSentence(v);
  const detUrl = safeUrl(d.detection_video_url);
  let withdraw = '';
  if (['uploading', 'queued', 'analysing', 'awaiting_review'].includes(st)) {
    withdraw = inReview
      ? `<form class="panel" id="withdraw-form"><div class="panel-title">Request withdrawal</div><p class="section-sub" style="margin-bottom:8px">A reviewer has already started on this clip, so withdrawal becomes a request they answer.</p><div class="field"><label>Why (required)</label><textarea class="textarea" name="reason" minlength="5" required></textarea></div><div class="modal-actions"><button class="btn btn-danger" type="submit">Send request</button></div></form>`
      : `<div class="action-row"><button class="btn btn-danger btn-sm" type="button" data-action="withdraw" data-id="${esc(v.id)}">Withdraw submission</button></div>`;
  }
  // Evidence JPEGs are reviewer-only (docs §2.6 24–25): the uploader gets the textual outcome per
  // finding and the redacted detection video, never an evidence frame or a blob path.
  setPage(`<button class="back-link" type="button" data-action="nav" data-page="submissions">← All submissions</button>
    <div class="panel"><div class="panel-title"><span style="min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(subName(v))}</span>${chip(st)}</div>
      <dl class="kv">
        <dt>uploaded</dt><dd>${esc(fmtDate(v.uploaded_at || v.created_at))}</dd>
        <dt>you reported</dt><dd>${esc(vioLabel(v.declared_violation))} · ${esc(vtLabel(v.vehicle_type))}${v.claimed_plate ? ` · plate <span class="mono">${esc(v.claimed_plate)}</span>` : ''}</dd>
        ${v.note ? `<dt>your note</dt><dd>${esc(v.note)}</dd>` : ''}
        ${v.recording_at || v.location_text ? `<dt>recorded</dt><dd>${esc(v.recording_at ? fmtDate(v.recording_at) : '')}${v.location_text ? (v.recording_at ? ' · ' : '') + esc(v.location_text) : ''}</dd>` : ''}
        <dt>status</dt><dd>${esc({ uploading: 'The file is still being uploaded.', queued: 'Waiting in line. Serious reports go first.', analysing: 'The AI is analysing every frame. This can take a few minutes.', awaiting_review: 'Analysed. A human reviewer decides next.', decided: 'A reviewer has decided every finding.', withdrawn: 'You withdrew this submission.', could_not_process: 'We could not process this file. Try a different export of the clip.' }[st] || '')}</dd>
      </dl>
      ${answer ? `<div class="reasoning-box"><b>What the AI says:</b> ${esc(answer)}</div>` : ''}
      ${withdraw}
    </div>
    ${detUrl ? `<div class="panel"><div class="panel-title">Detection video</div><p class="section-sub" style="margin-bottom:6px">What was detected, on which vehicle and why. Faces and other plates are blurred. This is the only footage you receive — the individual evidence frames stay with the reviewers.</p><video class="clip-video" controls preload="metadata" src="${esc(detUrl)}"></video></div>`
      : (st === 'awaiting_review' || decided ? `<div class="panel"><div class="panel-title">Detection video</div><p class="section-sub">You receive one redacted detection video once a reviewer has decided your vehicle's case. Individual evidence frames stay with the reviewers.</p></div>` : '')}
    ${subject ? `<div class="panel"><div class="panel-title"><span>Your vehicle${v.claimed_plate ? ` · <span class="plate">${esc(v.claimed_plate)}</span>` : ''}</span>${chip(subject.status)}</div>
      <div class="row-meta">${esc(vtLabel(subject.vehicle_type))}${subject.identity_status === 'conflict' ? ' · the plate the AI read differs from what you typed — a reviewer settles it' : ''}</div>
      ${list(subject.findings).length ? list(subject.findings).map(fd => findingRow(fd, decided)).join('') : '<div class="verdict-reason" style="margin-top:8px">No findings on this vehicle yet.</div>'}
    </div>` : (st === 'awaiting_review' || decided ? empty('No subject vehicle found', 'The AI could not match the vehicle you described. A reviewer will look at the clip by hand.') : '')}
    ${others.map((c, i) => `<div class="panel"><div class="panel-title"><span>Vehicle #${i + 2}</span>${chip(c.status)}</div><div class="row-meta">${esc(vtLabel(c.vehicle_type))} · plate not shown</div>${list(c.findings).map(fd => findingRow(fd, decided)).join('')}</div>`).join('')}
    <p class="decision-note" style="max-width:70ch">Provisional until a reviewer decides. Rejected findings stay on record as unconfirmed observations. You never see reviewer names or private notes.</p>`);
  document.getElementById('withdraw-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const reason = e.target.reason.value.trim();
    if (reason.length < 5) { showToast('Please give a reason (at least 5 characters)'); return; }
    try { await api(`/videos/${encodeURIComponent(v.id)}/withdraw`, { method: 'POST', body: { reason } }); showToast('Withdrawal request sent to the reviewer'); PAGE_FN.submission(v.id); }
    catch (err) { showToast(err.status === 409 ? 'This submission has already been decided and cannot be withdrawn.' : err.message); }
  });
};
async function withdrawSub(id) {
  if (!confirm('Withdraw this submission? It will be removed from the queue.')) return;
  try { await api(`/videos/${encodeURIComponent(id)}/withdraw`, { method: 'POST', body: {} }); showToast('Submission withdrawn'); PAGE_FN.submission(id); }
  catch (e) { showToast(e.status === 409 ? 'This submission has already been decided and cannot be withdrawn.' : e.message); }
}

PAGE_FN.help = function() {
  setPage(head('Help', 'What the words mean and what happens to your clip.') + `
    <div class="panel"><div class="panel-title">Statuses</div><dl class="kv">
      <dt>Uploading</dt><dd>Your file is still on its way to private storage.</dd>
      <dt>Queued</dt><dd>Waiting for the AI. Serious reports are checked first, then oldest first.</dd>
      <dt>Analysing</dt><dd>Every frame is being examined. Accuracy beats speed — this can take minutes.</dd>
      <dt>Awaiting review</dt><dd>The AI has reported. A human reviewer decides every finding.</dd>
      <dt>Decided</dt><dd>Every finding has a decision: confirmed, rejected (with a reason) or inconclusive.</dd>
      <dt>Withdrawn</dt><dd>You took the submission back.</dd>
      <dt>Could not process</dt><dd>The file could not be analysed. Try another export.</dd></dl></div>
    <div class="panel"><div class="panel-title">How a finding becomes real</div>
      <p class="tier-body">The AI recommends, a human decides, the traffic authority enforces. Nothing counts until a reviewer confirms it, and RoadWatch never issues or collects a fine. Rejected findings are kept on record as unconfirmed observations so patterns stay visible.</p></div>
    <div class="panel"><div class="panel-title">Privacy</div>
      <p class="tier-body">You see the plate you typed and nothing else. Other vehicles in your clip appear as "Vehicle #2, #3" with no plate. Evidence frames are never sent to you: you receive the outcome of every finding in words, plus one redacted detection video after the review. Reviewer identities and private notes are never shown to uploaders.</p></div>
    <div class="panel"><div class="panel-title">Withdrawing</div>
      <p class="tier-body">Before a reviewer starts, withdrawal is instant. Once review has started it becomes a request the reviewer answers. Decided submissions cannot be withdrawn.</p></div>`);
};

// ══════════════════════════════════════════════════════════
// ── REVIEWER portal ───────────────────────────────────────
// ══════════════════════════════════════════════════════════
async function loadReasons() {
  if (state.reasons) return state.reasons;
  try { state.reasons = list(await api('/rejection-reasons')); } catch (_) { state.reasons = Object.entries(REJECTION_FALLBACK).map(([code, label]) => ({ code, label })); }
  if (!state.reasons.length) state.reasons = Object.entries(REJECTION_FALLBACK).map(([code, label]) => ({ code, label }));
  return state.reasons;
}
// Two payload shapes: the list (`plate: {ai, confidence, claimed, corrected}`) and the case detail
// (`ai.resolved_plate` + `plate_of_record`, which already folds in the latest plate correction).
function plateOf(c) {
  const p = c.plate && typeof c.plate === 'object' ? c.plate : (c.plate ? { ai: c.plate } : {});
  const resolved = c.ai?.resolved_plate || {};
  const ai = p.ai ?? resolved.text ?? null;
  const record = c.plate_of_record ?? null;
  const corrected = p.corrected ?? (record && record !== ai ? record : null);
  return { text: corrected || ai || record, conf: p.confidence ?? resolved.confidence, claimed: p.claimed ?? c.uploader_claim?.claimed_plate ?? null, corrected, ai };
}
function caseCard(c, opts = {}) {
  const p = plateOf(c), mine = c.claimed_by && c.claimed_by === uid(), busy = c.claimed_by && !mine;
  const idle = c.idle_hours ?? (c.claimed_at ? (Date.now() - new Date(c.claimed_at)) / 36e5 : null);
  return `<article class="review-card">
    <div class="card-top"><div style="min-width:0">
      <div class="row-title">${esc(caseNo(c.id))} ${c.is_subject ? '<span class="chip chip-grey">subject</span>' : '<span class="chip chip-grey">other vehicle</span>'}</div>
      <div class="card-meta">${esc(vtLabel(c.vehicle_type))} · plate ${p.text ? `<span class="mono">${esc(p.text)}</span> (${esc(pct(p.conf))})` : 'not read'}${p.claimed && p.claimed !== p.text ? ` · claimed <span class="mono">${esc(p.claimed)}</span>` : ''}</div>
      <div class="card-meta">${esc(c.findings_count ?? list(c.findings).length)} finding(s) · priority ${esc(c.priority ?? 0)} · identity ${esc(c.identity_status || '—')} · ${esc(ago(c.created_at))}</div>
    </div><div class="row-right">${chip(c.status)}${c.allegation_answer === 'not_supported' || c.allegation_answer === 'unobservable' ? chip(c.allegation_answer) : ''}</div></div>
    ${idle != null && opts.idle ? `<div class="card-meta" style="margin-top:8px;color:${idle >= 24 ? '#c43818' : 'var(--navy60)'}">Claimed ${esc(ago(c.claimed_at))} · idle ${esc(idle.toFixed(1))} h${idle >= 24 ? ' — release or finish it' : ''}</div>` : ''}
    <div class="action-row" style="flex-wrap:wrap">
      ${mine || c.status === 'finalized' || isAdmin() ? `<button class="btn btn-sm" type="button" data-action="open-case" data-id="${esc(c.id)}">${mine ? 'Open' : 'View'}</button>` : ''}
      ${['pending_review', 'second_opinion'].includes(c.status) && !mine ? `<button class="btn btn-sm btn-signal" type="button" data-action="claim" data-id="${esc(c.id)}" ${busy ? 'disabled title="Claimed by another reviewer"' : ''}>${busy ? 'Claimed' : 'Claim'}</button>` : ''}
      ${mine && c.status === 'in_review' ? `<button class="btn btn-sm btn-ghost" type="button" data-action="release" data-id="${esc(c.id)}">Release</button>` : ''}
    </div></article>`;
}
PAGE_FN.queue = async function(_, silent) {
  if (!silent) setPage(head('Queue', 'Ordered by seriousness, then age. Claim a case to lock it to you (max 3 open).', `
    <div class="seg" id="queue-tabs"><button type="button" data-action="queue-tab" data-tab="normal">Normal</button><button type="button" data-action="queue-tab" data-tab="not_supported">AI: allegation not supported</button><button type="button" data-action="queue-tab" data-tab="second_opinion">Second opinion</button></div>
    <button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div class="review-grid" id="queue-list"><div class="spinner"></div></div>`);
  document.querySelectorAll('#queue-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === state.queueTab));
  const el = document.getElementById('queue-list'); if (!el) return;
  const qs = state.queueTab === 'second_opinion' ? 'status=second_opinion' : `lane=${state.queueTab}&status=pending_review`;
  try {
    const items = list(await api(`/cases?${qs}&limit=50`));
    if (currentPage !== 'queue') return;
    el.innerHTML = items.length ? items.map(c => caseCard(c)).join('') : `<div class="empty" style="grid-column:1/-1"><b>Queue is empty</b>New cases appear here in real time.</div>`;
  } catch (e) { el.innerHTML = `<div style="grid-column:1/-1">${failBox(e, 'refresh')}</div>`; }
};
PAGE_FN.mycases = async function(_, silent) {
  if (!silent) setPage(head('My cases', 'Cases locked to you. Claims idle for 24 h raise an alert.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div class="review-grid" id="mine-list"><div class="spinner"></div></div>`);
  const el = document.getElementById('mine-list'); if (!el) return;
  try {
    const items = list(await api('/cases/mine'));
    if (currentPage !== 'mycases') return;
    el.innerHTML = items.length ? items.map(c => caseCard(c, { idle: true })).join('') : `<div class="empty" style="grid-column:1/-1"><b>No open claims</b>Claim a case from the queue.</div>`;
    if (!pageTimer) pageTimer = setInterval(() => PAGE_FN.mycases(null, true), 60000);
  } catch (e) { el.innerHTML = `<div style="grid-column:1/-1">${failBox(e, 'refresh')}</div>`; }
};
PAGE_FN.completed = async function(_, silent) {
  if (!silent) setPage(head('Completed', 'Finalized cases. Locked; only an admin can reopen with a reason.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div class="review-grid" id="done-list"><div class="spinner"></div></div>`);
  const el = document.getElementById('done-list'); if (!el) return;
  try {
    const items = list(await api('/cases?status=finalized&limit=100'));
    if (currentPage !== 'completed') return;
    el.innerHTML = items.length ? items.map(c => caseCard(c)).join('') : `<div class="empty" style="grid-column:1/-1"><b>Nothing finalized yet</b></div>`;
  } catch (e) { el.innerHTML = `<div style="grid-column:1/-1">${failBox(e, 'refresh')}</div>`; }
};
PAGE_FN.stats = async function() {
  setPage(head('My stats', 'From the cases you finalized.') + `<div class="tiles" id="stats-tiles"><div class="spinner"></div></div><div id="stats-body"></div>`);
  try {
    const all = list(await api('/cases?status=finalized&limit=200'));
    const mine = all.filter(c => (c.finalized_by || c.claimed_by) === uid());
    const by = (k, f) => mine.reduce((m, c) => { const v = f(c); m[v] = (m[v] || 0) + 1; return m; }, {});
    const lanes = by('lane', c => c.lane || 'normal'), types = by('vt', c => vtLabel(c.vehicle_type));
    const avg = mine.length ? mine.reduce((s, c) => s + Math.max(0, (new Date(c.finalized_at || c.created_at) - new Date(c.claimed_at || c.created_at)) / 1000), 0) / mine.length : 0;
    document.getElementById('stats-tiles').innerHTML = tile(mine.length, 'Finalized by me') + tile(mine.reduce((s, c) => s + (c.findings_count || 0), 0), 'Findings decided')
      + tile(lanes.not_supported || 0, 'Not-supported lane') + tile(mine.length ? dur(avg) : '—', 'Avg time per case');
    document.getElementById('stats-body').innerHTML = `<div class="panel"><div class="panel-title">By vehicle type</div>${Object.keys(types).length ? Object.entries(types).map(([k, n]) => `<div class="data-row"><span>${esc(k)}</span><span class="data-val">${n}</span></div>`).join('') : empty('No finalized cases yet')}</div>`;
  } catch (e) { document.getElementById('stats-tiles').innerHTML = failBox(e); }
};
async function claimCase(id) {
  try { await api(`/cases/${encodeURIComponent(id)}/claim`, { method: 'POST' }); showToast('Case claimed — it is locked to you'); window.location.hash = '#case/' + id; }
  catch (e) { showToast(e.status === 409 ? 'Already claimed by someone else, or you hold 3 open claims.' : e.message); if (e.status === 409) reloadIf(['queue', 'mycases']); }
}
async function releaseCase(id) {
  if (!confirm('Release this case back to the queue?')) return;
  try { await api(`/cases/${encodeURIComponent(id)}/release`, { method: 'POST' }); showToast('Case released'); window.location.hash = isAdmin() ? '#cases' : '#queue'; }
  catch (e) { showToast(e.message); }
}

// ── Case screen ──
const signCache = new Map();
async function evidenceUrl(e) {
  const direct = safeUrl(e.url || e.signed_url);
  if (direct) return direct;
  const p = e.blob_path || e.path; if (!p) return null;
  if (!signCache.has(p)) signCache.set(p, api('/evidence/sign?path=' + encodeURIComponent(p)).then(r => safeUrl(r?.url || r?.signed_url) ).catch(() => null));
  return signCache.get(p);
}
function normEvidence(c) {
  const ev = c.evidence;
  if (Array.isArray(ev)) return ev;
  if (ev && typeof ev === 'object') return Object.entries(ev).flatMap(([fid, arr]) => list(arr).map(e => ({ ...e, finding_id: e.finding_id || fid })));
  return [];
}
function fmtVal(v) { return v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v); }
PAGE_FN.case = async function(id) {
  setPage(`<button class="back-link" type="button" data-action="nav" data-page="${isAdmin() ? 'cases' : 'mycases'}">← Back</button><div class="spinner"></div>`);
  let c = null, err = null;
  try { [c] = await Promise.all([api('/cases/' + encodeURIComponent(id)), loadReasons()]); } catch (e) { err = e; }
  if (currentPage !== 'case' || currentId !== id) return;
  if (!c || !c.id) { setPage(`<button class="back-link" type="button" data-action="nav" data-page="${isAdmin() ? 'cases' : 'mycases'}">← Back</button>${failBox(err || new Error('Could not load the case'))}`); return; }
  state.kase = c;
  const mine = c.claimed_by === uid() && c.status === 'in_review';
  const ai = c.ai || c; // API.md §3: AI block is nested under `ai`; fall back to flat for older payloads
  const findings = list(ai.findings);
  const evidence = normEvidence(c);
  const p = plateOf(c), claim = c.uploader_claim || {}, track = ai.track || ai.vehicle_track || list(ai.tracks || ai.vehicle_tracks).find(t => t.track_id === c.track_id) || {};
  const obs = list(ai.plate_observations), corrections = list(c.corrections), decisions = list(c.decisions), vlm = list(ai.vlm_calls), models = ai.model_versions || {};
  const resolved = ai.resolved_plate || {}, resolvedText = resolved.text ?? resolved.plate, pipelineVersion = c.pipeline_version ?? ai.pipeline_version;
  // The case-detail payload carries these under `ai.track` / `allegation`; the list payload carries them flat.
  const vehicleType = c.vehicle_type ?? track.vehicle_type ?? claim.vehicle_type ?? null;
  const allegationAnswer = c.allegation_answer ?? c.allegation?.answer ?? null;
  const wr = c.withdrawal_request || c.withdrawal || null;
  const pendingWr = wr && (wr.status || 'pending') === 'pending' ? wr : null;
  const hist = c.plate_history;
  const hasHistory = Array.isArray(hist) ? hist.length > 0 : !!hist && (list(hist.confirmed).length + list(hist.observed).length > 0 || !!hist.plate);
  const allDecided = findings.length > 0 && findings.every(f => f.decision && f.decision !== 'pending');
  const vehicleOpts = ['two_wheeler', 'four_wheeler'].map(t => `<option value="${t}">${vtLabel(t)}</option>`).join('');
  const vioOpts = Object.entries(VIOLATION_POLICY).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
  const reasonOpts = '<option value="">Pick a reason…</option>' + state.reasons.map(r => `<option value="${esc(r.code)}">${esc(r.label)}</option>`).join('');

  setPage(`<button class="back-link" type="button" data-action="nav" data-page="${isAdmin() ? 'cases' : 'mycases'}">← Back</button>
    <div class="page-head" style="padding-top:8px"><div><h2 class="section-title">${esc(caseNo(c.id))} <span style="font-size:14px;font-weight:500;color:var(--navy60)">${c.is_subject ? 'subject vehicle' : 'other vehicle'} · ${esc(c.lane === 'not_supported' ? 'AI: allegation not supported' : 'normal lane')} · cycle ${esc(c.cycle ?? 1)}</span></h2>
      <p class="section-sub">${mine ? 'Locked to you. Decide every finding, then finalize.' : c.status === 'finalized' ? 'Finalized and locked.' : c.claimed_by ? 'Claimed by another reviewer — read only.' : 'Not claimed — read only.'}</p></div>
      <div class="page-actions">${chip(c.status)}${mine ? `<button class="btn btn-sm btn-ghost" type="button" data-action="release" data-id="${esc(c.id)}">Release</button>` : ''}${!mine && ['pending_review', 'second_opinion'].includes(c.status) && !c.claimed_by ? `<button class="btn btn-sm btn-signal" type="button" data-action="claim" data-id="${esc(c.id)}">Claim</button>` : ''}</div></div>

    ${pendingWr ? `<div class="panel" style="border-color:var(--amber)"><div class="panel-title"><span>Withdrawal requested</span><span class="chip chip-amber">Needs an answer</span></div>
      <p class="section-sub">The uploader asked to take this submission back${pendingWr.created_at ? ' · ' + esc(ago(pendingWr.created_at)) : ''}. Accepting closes the submission and every open case on it. Declining keeps the review going; the uploader is told your reason.</p>
      ${pendingWr.reason ? `<div class="reasoning-box"><b>Their reason:</b> ${esc(pendingWr.reason)}</div>` : ''}
      ${mine || isAdmin() ? `<div class="action-row"><button class="btn btn-sm btn-danger" type="button" data-action="wr-accept" data-id="${esc(c.id)}">Accept — close it</button><button class="btn btn-sm" type="button" data-action="wr-decline" data-id="${esc(c.id)}">Decline…</button></div>`
        : '<div class="row-meta">Only the reviewer holding this case, or an admin, can answer it.</div>'}</div>` : ''}

    <!-- 1. AI original block (immutable) -->
    <div class="panel"><div class="panel-title"><span>1 · AI original</span><span class="chip chip-grey">immutable</span></div>
      <dl class="kv">
        <dt>uploader claim</dt><dd>${esc(vioLabel(claim.declared_violation))}${claim.claimed_plate ? ` · plate <span class="mono">${esc(claim.claimed_plate)}</span>` : ' · no plate given'}</dd>
        ${claim.note ? `<dt>uploader note</dt><dd>${esc(claim.note)}</dd>` : ''}
        <dt>vehicle</dt><dd>${esc(vtLabel(vehicleType))}${track.vehicle_class ? ` (${esc(track.vehicle_class)} ${esc(pct(track.class_confidence))})` : ''} · track #${esc(c.track_id)}${track.frames_observed != null ? ` · ${esc(track.frames_observed)} frames · ${esc(Number(track.first_seen || 0).toFixed(1))}s → ${esc(Number(track.last_seen || 0).toFixed(1))}s` : ''}</dd>
        <dt>identity</dt><dd>${chip(c.identity_status || 'provisional')}</dd>
        <dt>plate (AI)</dt><dd><span class="plate">${esc(p.text || resolvedText || 'not read')}</span> ${p.text || resolvedText ? `· ${esc(pct(p.conf ?? resolved.confidence))}${resolved.method ? ' · ' + esc(resolved.method) : ''}` : ''}${p.corrected ? ` · <span class="chip chip-amber">corrected to ${esc(p.corrected)}</span>` : ''}</dd>
        ${obs.length ? `<dt>plate reads</dt><dd>${obs.map(o => `<span class="tag mono">${esc(o.text)} · ${esc(o.engine)} · ${esc(pct(o.confidence))}${o.frame_index != null ? ' · f' + esc(o.frame_index) : ''}</span>`).join(' ')}</dd>` : ''}
        <dt>allegation</dt><dd>${allegationAnswer ? chip(allegationAnswer) : '—'}</dd>
        ${Object.keys(models).length ? `<dt>models</dt><dd class="mono" style="font-size:11px">${esc(Object.entries(models).map(([k, v]) => k + '=' + v).join(' · '))}${pipelineVersion ? ' · pipeline ' + esc(pipelineVersion) : ''}</dd>` : ''}
        ${vlm.length ? `<dt>tiebreaker</dt><dd>${vlm.map(x => `${esc(x.model || 'vlm')} on ${esc(humanType(x.scope))}: ${esc(x.error ? 'error — ' + x.error : (x.output || '—'))}`).join('<br>')}</dd>` : ''}
      </dl>
      ${findings.map(f => `<div class="verdict"><div class="verdict-head"><span class="verdict-type">${esc(vioLabel(f.violation))}</span><span>${chip(f.tier)} ${chip(f.ai_result || f.result)} <span class="mono" style="font-size:10.5px;color:var(--navy60)">conf ${esc(pct(f.confidence))} · agree ${esc(pct(f.agreement))} · ${esc(f.evidence_frames ?? 0)}/${esc(f.evaluable_frames ?? 0)} frames</span></span></div>${f.reasoning ? `<div class="verdict-reason">${esc(f.reasoning)}</div>` : ''}</div>`).join('') || empty('No findings on this case')}
    </div>

    <!-- 2. Evidence per finding -->
    <div class="panel"><div class="panel-title"><span>2 · Evidence</span><span class="mono" style="font-size:10px;color:var(--navy40)">signed links · 15 min</span></div>
      ${findings.map(f => `<div class="ev-group" data-finding="${esc(f.id)}"><div class="verdict-type" style="margin:8px 0 4px">${esc(vioLabel(f.violation))}</div><div class="ev-big" data-ev-big></div><div class="scrub" data-ev-strip><div class="spinner"></div></div></div>`).join('') || empty('No evidence')}
    </div>

    <!-- 3. Corrections -->
    <div class="panel"><div class="panel-title">3 · Corrections <span class="mono" style="font-size:10px;color:var(--navy40)">sit beside the AI values, never replace them</span></div>
      ${corrections.length ? corrections.map(x => `<div class="data-row"><span>${esc(humanType(x.field))}${x.finding_id ? ' · ' + esc(vioLabel(findings.find(f => f.id === x.finding_id)?.violation) || short(x.finding_id)) : ''}</span><span class="data-val">${esc(fmtVal(x.ai_value))} → ${esc(fmtVal(x.corrected_value))}${x.reason ? ' · ' + esc(x.reason) : ''}</span></div>`).join('') : '<div class="row-meta">No corrections yet.</div>'}
      ${mine ? `<form id="corr-form" class="two-col" style="margin-top:12px">
        <div class="field"><label>Corrected plate (reason required)</label><input class="input mono" name="plate" placeholder="${esc(p.text || 'MH12AB1234')}" autocapitalize="characters" maxlength="12"><input class="input" name="plate_reason" placeholder="Why — e.g. 8 read as B on the crop" style="margin-top:6px"></div>
        <div class="field"><label>Corrected vehicle type</label><select class="select" name="vehicle_type"><option value="">Keep ${esc(vtLabel(vehicleType))}</option>${vehicleOpts}</select></div>
        ${findings.map(f => `<div class="field"><label>Label for "${esc(vioLabel(f.violation))}"</label><select class="select" name="label-${esc(f.id)}"><option value="">Keep</option>${vioOpts}</select></div>`).join('')}
        <div class="field" style="grid-column:1/-1"><label>Reason (optional for type / label)</label><input class="input" name="reason" placeholder="Short reason"></div>
        <div class="modal-actions" style="grid-column:1/-1"><button class="btn" type="submit">Save corrections</button></div></form>` : ''}
    </div>

    <!-- 4. Decisions -->
    <div class="panel"><div class="panel-title">4 · Decisions <span class="mono" style="font-size:10px;color:var(--navy40)">per finding · append-only</span></div>
      ${findings.map(f => `<div class="verdict" data-decision-row="${esc(f.id)}"><div class="verdict-head"><span class="verdict-type">${esc(vioLabel(f.violation))}</span><span data-dec-chip>${chip(f.decision || 'pending')}${f.decision === 'rejected' && f.rejection_reason ? ` <span class="tag">${esc(reasonLabel(f.rejection_reason))}</span>` : ''}</span></div>
        ${f.note ? `<div class="verdict-reason">Note: ${esc(f.note)}</div>` : ''}
        ${mine ? `<form class="dec-form" data-fid="${esc(f.id)}">
          <div class="seg" style="margin:8px 0"><button type="button" data-dec="confirmed" class="${f.decision === 'confirmed' ? 'active' : ''}">Confirm</button><button type="button" data-dec="rejected" class="${f.decision === 'rejected' ? 'active' : ''}">Reject</button><button type="button" data-dec="inconclusive" class="${f.decision === 'inconclusive' ? 'active' : ''}">Inconclusive</button></div>
          <div class="field" data-reason-wrap style="display:${f.decision === 'rejected' ? 'block' : 'none'}"><label>Rejection reason (required)</label><select class="select" name="rejection_reason">${reasonOpts}</select></div>
          <div class="field"><label>Note (optional)</label><input class="input" name="note" maxlength="400" placeholder="Short note for the record"></div>
          <div class="modal-actions" style="margin-top:4px"><button class="btn btn-sm" type="submit">Save decision</button></div></form>` : ''}
      </div>`).join('') || empty('No findings to decide')}
      ${decisions.length ? `<details style="margin-top:10px"><summary class="row-meta" style="cursor:pointer">Decision history (${decisions.length})</summary>${decisions.map(d => `<div class="data-row"><span>${esc(vioLabel(findings.find(f => f.id === d.finding_id)?.violation) || short(d.finding_id))} · cycle ${esc(d.cycle ?? 1)} · ${esc(ago(d.created_at))}</span><span class="data-val">${esc(d.decision)}${d.rejection_reason ? ' · ' + esc(reasonLabel(d.rejection_reason)) : ''}${d.note ? ' · ' + esc(d.note) : ''}</span></div>`).join('')}</details>` : ''}
      ${mine ? `<div class="action-row"><button class="btn btn-green" id="btn-finalize" type="button" data-action="finalize" data-id="${esc(c.id)}" ${allDecided ? '' : 'disabled'} style="flex:1">Finalize case</button></div><div class="decision-note" id="finalize-note">${allDecided ? 'Every finding has a decision. Finalizing locks the case; inconclusive findings go to a second reviewer.' : 'Finalize unlocks once every finding has a decision.'}</div>` : ''}
    </div>
    ${hasHistory ? plateHistoryBlock(c.plate_history, c.plate_of_record || p.text) : (c.status !== 'finalized' && !isAdmin() ? '<p class="decision-note">Plate history is shown after you finalize, so the past does not decide the present.</p>' : '')}`);

  renderEvidence(c, findings, evidence, mine);
  if (mine) wireCaseForms(c, findings, evidence);
};
async function renderEvidence(c, findings, evidence, mine) {
  for (const f of findings) {
    const group = document.querySelector(`.ev-group[data-finding="${CSS.escape(f.id)}"]`); if (!group) continue;
    const items = evidence.filter(e => e.finding_id === f.id);
    const urls = await Promise.all(items.map(evidenceUrl));
    if (state.kase !== c) return;
    const strip = group.querySelector('[data-ev-strip]'), big = group.querySelector('[data-ev-big]');
    const ok = items.map((e, i) => ({ e, url: urls[i] })).filter(x => x.url);
    if (!ok.length) { strip.innerHTML = '<div class="row-meta">No evidence frames could be loaded.</div>'; continue; }
    const sel = state.evidenceSel[f.id] = new Set(ok.map(x => x.e.id || x.e.blob_path || x.e.path));
    strip.innerHTML = ok.map((x, i) => { const key = x.e.id || x.e.blob_path || x.e.path; return `<label class="thumb ${i === 0 ? 'current' : ''}" data-key="${esc(key)}"><img src="${esc(x.url)}" alt="frame ${esc(x.e.frame_index ?? i)}" loading="lazy" data-url="${esc(x.url)}"><span class="thumb-cap">f${esc(x.e.frame_index ?? i)}${x.e.timestamp != null ? ' · ' + esc(Number(x.e.timestamp).toFixed(1)) + 's' : ''}</span>${mine ? `<input type="checkbox" checked title="Keep this frame as evidence">` : ''}</label>`; }).join('')
      + (mine ? `<button class="btn btn-sm" type="button" data-action="save-evidence" data-fid="${esc(f.id)}" style="align-self:center;flex:none">Save selection</button>` : '');
    big.innerHTML = `<a href="${esc(ok[0].url)}" target="_blank" rel="noopener"><img src="${esc(ok[0].url)}" alt="Evidence frame"></a>`;
    strip.addEventListener('click', e => {
      const th = e.target.closest('.thumb'); if (!th) return;
      if (e.target.type === 'checkbox') { e.target.checked ? sel.add(th.dataset.key) : sel.delete(th.dataset.key); th.classList.toggle('dropped', !e.target.checked); return; }
      e.preventDefault();
      strip.querySelectorAll('.thumb').forEach(t => t.classList.toggle('current', t === th));
      big.innerHTML = `<a href="${esc(th.querySelector('img').dataset.url)}" target="_blank" rel="noopener"><img src="${esc(th.querySelector('img').dataset.url)}" alt="Evidence frame"></a>`;
    });
  }
}
function wireCaseForms(c, findings, evidence) {
  const cid = encodeURIComponent(c.id);
  document.getElementById('corr-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target, calls = [], reason = f.reason.value.trim();
    const plate = f.plate.value.toUpperCase().replace(/[\s-]+/g, '');
    if (plate) {
      if (!PLATE_RE.test(plate)) { showToast('Corrected plate must be a valid Indian plate'); return; }
      if (f.plate_reason.value.trim().length < 3) { showToast('A reason is required for a plate correction'); return; }
      calls.push({ field: 'plate', ai_value: plateOf(c).ai, corrected_value: plate, reason: f.plate_reason.value.trim() });
    }
    const vt = c.vehicle_type ?? c.ai?.track?.vehicle_type ?? null;
    if (f.vehicle_type.value && f.vehicle_type.value !== vt) calls.push({ field: 'vehicle_type', ai_value: vt, corrected_value: f.vehicle_type.value, reason });
    findings.forEach(fd => { const v = f[`label-${fd.id}`]?.value; if (v && v !== fd.violation) calls.push({ field: 'violation_label', finding_id: fd.id, ai_value: fd.violation, corrected_value: v, reason }); });
    if (!calls.length) { showToast('Nothing to correct'); return; }
    const btn = f.querySelector('[type=submit]'); btn.disabled = true;
    try { for (const body of calls) await api(`/cases/${cid}/corrections`, { method: 'POST', body }); showToast(`${calls.length} correction(s) saved`); PAGE_FN.case(c.id); }
    catch (err) { btn.disabled = false; showToast(err.message); }
  });
  document.querySelectorAll('.dec-form').forEach(form => {
    let decision = findings.find(x => x.id === form.dataset.fid)?.decision; if (decision === 'pending') decision = null;
    form.querySelectorAll('[data-dec]').forEach(b => b.addEventListener('click', () => {
      decision = b.dataset.dec;
      form.querySelectorAll('[data-dec]').forEach(x => x.classList.toggle('active', x === b));
      form.querySelector('[data-reason-wrap]').style.display = decision === 'rejected' ? 'block' : 'none';
    }));
    form.addEventListener('submit', async e => {
      e.preventDefault();
      if (!decision) { showToast('Pick confirm, reject or inconclusive'); return; }
      const body = { decision };
      if (decision === 'rejected') { if (!form.rejection_reason.value) { showToast('Pick a rejection reason'); return; } body.rejection_reason = form.rejection_reason.value; }
      if (form.note.value.trim()) body.note = form.note.value.trim();
      const btn = form.querySelector('[type=submit]'); btn.disabled = true;
      try {
        await api(`/cases/${cid}/findings/${encodeURIComponent(form.dataset.fid)}/decision`, { method: 'POST', body });
        const fd = findings.find(x => x.id === form.dataset.fid); if (fd) { fd.decision = decision; fd.rejection_reason = body.rejection_reason || null; }
        form.closest('[data-decision-row]').querySelector('[data-dec-chip]').innerHTML = chip(decision) + (body.rejection_reason ? ` <span class="tag">${esc(reasonLabel(body.rejection_reason))}</span>` : '');
        const all = findings.every(x => x.decision && x.decision !== 'pending');
        const fin = document.getElementById('btn-finalize'); if (fin) fin.disabled = !all;
        const note = document.getElementById('finalize-note'); if (note) note.textContent = all ? 'Every finding has a decision. Finalizing locks the case; inconclusive findings go to a second reviewer.' : 'Finalize unlocks once every finding has a decision.';
        showToast('Decision saved');
      } catch (err) { showToast(err.status === 409 ? 'This case is no longer locked to you.' : err.message); }
      finally { btn.disabled = false; }
    });
  });
}
// The evidence key: the evidence row id, falling back to its blob path. One notion, used for the
// selection Set and for the correction body — CorrectionRequest accepts a list here
// (Union[str, List[str]] in backend/app/schemas/rbac.py), so the frames travel as arrays of keys.
const evKey = e => String(e.id || e.blob_path || e.path || '');
async function saveEvidence(fid) {
  const c = state.kase; if (!c) return;
  const rows = normEvidence(c).filter(e => e.finding_id === fid);
  const kept = rows.filter(e => state.evidenceSel[fid]?.has(evKey(e)));
  if (kept.length === rows.length) { showToast('No frames were unselected'); return; }
  if (!kept.length && !confirm('Unselect every frame for this finding?')) return;
  const reason = prompt('Why are these frames unselected? (optional)') ?? '';
  const body = { field: 'evidence', finding_id: fid, ai_value: rows.map(evKey), corrected_value: kept.map(evKey), reason: reason.trim() || undefined };
  try { await api(`/cases/${encodeURIComponent(c.id)}/corrections`, { method: 'POST', body }); showToast('Evidence selection saved'); PAGE_FN.case(c.id); }
  catch (e) { showToast(e.message); }
}
// The claiming reviewer (or an admin) answers the uploader's withdrawal request. Declining needs a reason.
async function answerWithdrawal(id, accept) {
  const post = body => api(`/cases/${encodeURIComponent(id)}/withdrawal`, { method: 'POST', body });
  const oops = e => showToast(e.status === 404 ? 'No withdrawal request is pending on this submission.' : e.status === 409 ? 'This case is no longer locked to you.' : e.message);
  if (accept) {
    if (!confirm('Accept the withdrawal? The submission and every open case on it close as withdrawn.')) return;
    try { await post({ accept: true }); showToast('Withdrawal accepted — the submission is closed'); window.location.hash = isAdmin() ? '#cases' : '#mycases'; }
    catch (e) { oops(e); }
    return;
  }
  openModal(`<h3>Decline the withdrawal</h3><p class="sub">The review continues. The uploader is told your reason.</p><form id="m-form"><div class="field"><label>Reason (required)</label><textarea class="textarea" name="reason" minlength="5" required placeholder="e.g. the review is nearly finished and the finding is top tier"></textarea></div><div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Cancel</button><button class="btn btn-signal" type="submit">Decline</button></div></form>`);
  document.getElementById('m-form').onsubmit = async e => {
    e.preventDefault();
    const reason = e.target.reason.value.trim();
    if (reason.length < 5) { showToast('Reason must be at least 5 characters'); return; }
    try { await post({ accept: false, reason }); closeModal(); showToast('Withdrawal declined'); PAGE_FN.case(id); }
    catch (err) { oops(err); }
  };
}
async function finalizeCase(id) {
  if (!confirm('Finalize this case? It locks; only an admin can reopen it.')) return;
  try {
    const r = await api(`/cases/${encodeURIComponent(id)}/finalize`, { method: 'POST' });
    showToast(r?.status === 'second_opinion' ? 'Sent for a second opinion' : 'Case finalized');
    window.location.hash = isAdmin() ? '#cases' : '#completed';
  } catch (e) { showToast(e.status === 422 ? 'Every finding needs a decision first.' : e.message); }
}
// Tolerates both shapes: {confirmed, observed} (case detail, /plates/{p}/history) and a flat
// plate_history array, which is bucketed by row.layer. `plate` fills in when the payload omits it.
function plateHistoryBlock(h, plate) {
  if (Array.isArray(h)) h = { confirmed: h.filter(r => r.layer === 'confirmed'), observed: h.filter(r => r.layer !== 'confirmed'), plate: h[0]?.plate };
  h = h || {};
  if (!h.plate && plate) h = { ...h, plate };
  const rows = (arr, layer) => list(arr).map(x => `<tr><td>${esc(layer)}</td><td>${esc(vioLabel(x.violation))}</td><td class="mono">${esc(short(x.case_id))}</td><td>${esc(ago(x.created_at))}</td>${isReviewer() ? `<td>${x.case_id ? `<button class="btn btn-sm btn-ghost" type="button" data-action="open-case" data-id="${esc(x.case_id)}">Open</button>` : ''}</td>` : ''}</tr>`);
  const all = [...rows(h.confirmed, 'confirmed'), ...rows(h.observed, 'observed')];
  // Only /plates/{p}/history carries the computed status; GET /cases/{id} omits it, and a hardcoded
  // "Clean" next to confirmed rows would be a lie. No status → no chip.
  return `<div class="panel"><div class="panel-title"><span>Plate history · <span class="plate">${esc(h.plate || '')}</span></span>${h.status ? chip(h.status) : ''}</div>
    <div class="row-meta" style="margin-bottom:8px">${esc(list(h.confirmed).length)} confirmed · ${esc(list(h.observed).length)} unconfirmed AI observations · ${esc(list(h.escalations).length)} escalation(s)</div>
    ${all.length ? table(['layer', 'violation', 'case', 'when', ...(isReviewer() ? [''] : [])], all) : '<div class="row-meta">Clean — nothing on record.</div>'}
    ${list(h.escalations).length ? list(h.escalations).map(e => `<div class="data-row"><span>escalation ${esc(short(e.id))} · ${esc(ago(e.created_at))}</span><span class="data-val">${esc(e.status)}</span></div>`).join('') : ''}</div>`;
}

// ══════════════════════════════════════════════════════════
// ── ADMIN portal ──────────────────────────────────────────
// ══════════════════════════════════════════════════════════
PAGE_FN.live = async function(_, silent) {
  if (!silent) setPage(head('Live board', 'Refreshes every 30 s.', `<span class="chip chip-grey" id="live-chip">Connecting</span><button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`)
    + `<div class="tiles" id="live-tiles"><div class="spinner"></div></div><div class="two-col"><div class="panel"><div class="panel-title">Processing now</div><div id="live-proc"></div></div><div class="panel"><div class="panel-title">Reviewers active</div><div id="live-rev"></div></div></div>`);
  if (!pageTimer) pageTimer = setInterval(() => PAGE_FN.live(null, true), 30000);
  const tiles = document.getElementById('live-tiles'); if (!tiles) return;
  try {
    const d = await api('/admin/live');
    if (currentPage !== 'live') return;
    const bp = d.queue?.by_priority || {}, rv = d.review || {};
    const wsAge = d.worker_last_seen ? (Date.now() - new Date(d.worker_last_seen)) / 1000 : Infinity;
    tiles.innerHTML = tile(d.users_online ?? 0, 'Users online', 'seen in last 5 min') + tile(d.uploads_today ?? 0, 'Uploads today')
      + tile(d.queue?.depth ?? 0, 'Queue depth', `top ${bp[3] ?? 0} · mid ${bp[2] ?? 0} · minor ${bp[1] ?? 0} · none ${bp[0] ?? 0}`, d.queue?.depth ? 'var(--amber)' : null)
      + tile(list(d.processing).length, 'Processing now') + tile(wsAge < 600 ? 'OK' : 'Stale', 'Worker heartbeat', d.worker_last_seen ? ago(d.worker_last_seen) : 'never seen', wsAge < 600 ? 'var(--green)' : 'var(--signal)')
      + tile(d.failed_today ?? 0, 'Failures today', null, d.failed_today ? 'var(--signal)' : null)
      + tile(rv.backlog ?? 0, 'Review backlog', rv.oldest_waiting_s != null ? 'oldest waiting ' + dur(rv.oldest_waiting_s) : null, rv.backlog ? 'var(--amber)' : null) + tile(rv.claimed ?? 0, 'Claimed');
    document.getElementById('live-proc').innerHTML = list(d.processing).length ? table(['clip', 'elapsed', 'attempts', ''], list(d.processing).map(x => `<tr><td class="mono">${esc(short(x.video_id))}</td><td>${esc(dur(x.elapsed_s))}</td><td>${esc(x.attempts ?? 0)}</td><td><button class="btn btn-sm btn-ghost" type="button" data-action="open-sub" data-id="${esc(x.video_id)}">Open</button></td></tr>`)) : '<div class="row-meta">Idle.</div>';
    document.getElementById('live-rev').innerHTML = list(rv.reviewers_active).length ? list(rv.reviewers_active).map(r => `<div class="data-row"><span>${esc(r.name || short(r.id))}</span><span class="data-val">${esc(r.open_claims ?? 0)} open</span></div>`).join('') : '<div class="row-meta">No reviewer has an open claim.</div>';
    setLive(PREVIEW_USER ? 'Preview' : channels.length ? 'Live' : null);
  } catch (e) { tiles.innerHTML = failBox(e, 'refresh'); }
};
PAGE_FN.processing = async function(_, silent) {
  if (!silent) setPage(head('Processing', 'Queue health and worker operations. Every action is audited.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button><button class="btn btn-sm" type="button" id="btn-pause" data-action="admin-pause">Pause intake</button><button class="btn btn-sm btn-danger" type="button" data-action="admin-retry-failed">Retry all failed</button>`)
    + `<div class="tiles" id="admin-tiles"><div class="spinner"></div></div><div class="panel"><div class="panel-title">Processing now</div><div id="admin-processing"></div></div><div class="panel"><div class="panel-title">Failed clips</div><div id="admin-failed"></div></div>`);
  const tiles = document.getElementById('admin-tiles'); if (!tiles) return;
  try {
    const qh = await api('/admin/queue');
    if (currentPage !== 'processing') return;
    state.admin = qh;
    const c = qh.counts || {};
    tiles.innerHTML = tile(c.uploading ?? 0, 'Uploading') + tile(c.unprocessed ?? 0, 'Queued', 'oldest: ' + (qh.oldest_unprocessed_at ? ago(qh.oldest_unprocessed_at) : 'none'), c.unprocessed ? 'var(--amber)' : null)
      + tile(c.processing ?? 0, 'Processing') + tile(c.processed ?? 0, 'Processed', null, 'var(--green)') + tile(c.failed ?? 0, 'Failed', null, c.failed ? 'var(--signal)' : null)
      + tile(qh.paused ? 'Paused' : 'Running', 'Intake', null, qh.paused ? 'var(--signal)' : 'var(--green)') + tile(ago(qh.worker_last_seen), 'Worker last seen', qh.worker_last_seen ? fmtDate(qh.worker_last_seen) : 'never');
    const pauseBtn = document.getElementById('btn-pause');
    if (pauseBtn) { pauseBtn.textContent = qh.paused ? 'Resume intake' : 'Pause intake'; pauseBtn.classList.toggle('btn-signal', !!qh.paused); }
    const proc = list(qh.processing), failed = list(qh.failed);
    document.getElementById('admin-processing').innerHTML = proc.length ? table(['clip', 'priority', 'claimed', 'attempts', ''], proc.map(v => `<tr><td class="mono">${esc(short(v.id))}</td><td>${esc(v.priority ?? 0)}</td><td>${esc(ago(v.claimed_at))}</td><td>${esc(v.attempts ?? 0)}</td><td><button class="btn btn-sm btn-ghost" type="button" data-action="open-sub" data-id="${esc(v.id)}">Open</button></td></tr>`)) : '<div class="row-meta">Nothing is being processed right now.</div>';
    document.getElementById('admin-failed').innerHTML = failed.length ? table(['clip', 'category', 'error', 'attempts', 'failed', ''], failed.map(v => `<tr><td class="mono">${esc(short(v.id))}</td><td>${chip(v.error_category || 'unknown')}</td><td class="wrap" style="color:#c43818">${esc(v.error_reason || 'unknown')}</td><td>${esc(v.attempts ?? 0)}</td><td>${esc(ago(v.processed_at || v.updated_at || v.uploaded_at))}</td><td><button class="btn btn-sm" type="button" data-action="requeue" data-id="${esc(v.id)}">Requeue</button></td></tr>`)) : '<div class="row-meta">No failed clips.</div>';
  } catch (e) { tiles.innerHTML = failBox(e, 'refresh'); }
};
PAGE_FN.cases = async function(_, silent) {
  if (!silent) setPage(head('Cases', 'Every case. Reopen with a reason; reassign releases and pre-assigns.', `
    <select class="select" id="cases-status" style="width:auto"><option value="">All statuses</option>${['pending_review', 'in_review', 'second_opinion', 'finalized', 'reopened'].map(s => `<option value="${s}">${CHIP[s][1]}</option>`).join('')}</select>
    <select class="select" id="cases-lane" style="width:auto"><option value="">All lanes</option><option value="normal">Normal</option><option value="not_supported">Not supported</option></select>
    <button class="btn btn-sm" type="button" data-action="refresh">Apply</button>`) + `<div id="cases-list"><div class="spinner"></div></div>`);
  const el = document.getElementById('cases-list'); if (!el) return;
  const st = document.getElementById('cases-status')?.value, lane = document.getElementById('cases-lane')?.value;
  try {
    const items = list(await api(`/cases?limit=100${st ? '&status=' + st : ''}${lane ? '&lane=' + lane : ''}`));
    if (currentPage !== 'cases') return;
    el.innerHTML = items.length ? table(['case', 'lane', 'status', 'vehicle', 'plate', 'findings', 'claimed by', 'age', ''], items.map(c => { const p = plateOf(c); return `<tr><td class="mono">${esc(short(c.id))}${c.is_subject ? ' ·S' : ''}</td><td>${esc(c.lane || 'normal')}</td><td>${chip(c.status)}</td><td>${esc(vtLabel(c.vehicle_type))}</td><td class="mono">${esc(p.text || '—')}</td><td>${esc(c.findings_count ?? 0)}</td><td class="mono">${esc(short(c.claimed_by) || '—')}</td><td>${esc(ago(c.created_at))}</td>
      <td style="white-space:nowrap"><button class="btn btn-sm btn-ghost" type="button" data-action="open-case" data-id="${esc(c.id)}">View</button>${c.status === 'finalized' ? `<button class="btn btn-sm" type="button" data-action="reopen" data-id="${esc(c.id)}">Reopen</button>` : `<button class="btn btn-sm" type="button" data-action="reassign" data-id="${esc(c.id)}">Reassign</button>`}${c.claimed_by ? `<button class="btn btn-sm btn-ghost" type="button" data-action="release" data-id="${esc(c.id)}">Release</button>` : ''}</td></tr>`; })) : empty('No cases match');
  } catch (e) { el.innerHTML = failBox(e, 'refresh'); }
};
function reopenCase(id) {
  openModal(`<h3>Reopen ${esc(caseNo(id))}</h3><p class="sub">Starts a new review cycle. Original decisions stay in the audit history.</p><form id="m-form"><div class="field"><label>Reason (required)</label><textarea class="textarea" name="reason" minlength="5" required></textarea></div><div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Cancel</button><button class="btn btn-signal" type="submit">Reopen</button></div></form>`);
  document.getElementById('m-form').onsubmit = async e => {
    e.preventDefault(); const reason = e.target.reason.value.trim();
    if (reason.length < 5) { showToast('Reason must be at least 5 characters'); return; }
    try { await api(`/admin/cases/${encodeURIComponent(id)}/reopen`, { method: 'POST', body: { reason } }); closeModal(); showToast('Case reopened'); PAGE_FN.cases(null, true); } catch (err) { showToast(err.message); }
  };
}
async function reassignCase(id) {
  let users = [];
  try { users = list(await api('/admin/users')).filter(u => ['officer', 'admin'].includes(u.role) && !u.deactivated_at); } catch (e) { showToast(e.message); return; }
  openModal(`<h3>Reassign ${esc(caseNo(id))}</h3><p class="sub">Releases the current claim and pre-assigns the case.</p><form id="m-form"><div class="field"><label>Reviewer</label><select class="select" name="reviewer_id" required><option value="">Pick a reviewer…</option>${users.map(u => `<option value="${esc(u.id)}">${esc(u.full_name || u.email)} · ${roleLabel(u.role)}</option>`).join('')}</select></div><div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Cancel</button><button class="btn btn-signal" type="submit">Reassign</button></div></form>`);
  document.getElementById('m-form').onsubmit = async e => {
    e.preventDefault(); const rid = e.target.reviewer_id.value; if (!rid) return;
    try { await api(`/admin/cases/${encodeURIComponent(id)}/reassign`, { method: 'POST', body: { reviewer_id: rid } }); closeModal(); showToast('Case reassigned'); PAGE_FN.cases(null, true); } catch (err) { showToast(err.message); }
  };
}
PAGE_FN.quality = async function() {
  setPage(head('Model quality', 'Last 30 days, computed from reviewer decisions and corrections.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div class="tiles" id="q-tiles"><div class="spinner"></div></div><div class="two-col"><div class="panel"><div class="panel-title">Rejection reasons</div><div id="q-reasons"></div></div><div class="panel"><div class="panel-title">AI vs reviewer agreement per violation</div><div id="q-agree"></div></div></div>`);
  await loadReasons();
  try {
    const d = await api('/admin/quality?days=30');
    if (currentPage !== 'quality') return;
    document.getElementById('q-tiles').innerHTML = tile(pct(d.plate_misread_rate), 'Plate misread rate', 'plate corrections / cases') + tile(pct(d.not_supported_rate), 'Not-supported rate', 'allegations the AI rejected') + tile(d.avg_review_seconds != null ? dur(d.avg_review_seconds) : '—', 'Avg review time');
    const rr = Object.entries(d.rejections_by_reason || {}).sort((a, b) => b[1] - a[1]), max = Math.max(1, ...rr.map(x => x[1]));
    document.getElementById('q-reasons').innerHTML = rr.length ? rr.map(([k, n]) => `<div class="bar-row"><span class="bar-label">${esc(reasonLabel(k))}</span><span class="bar"><i style="width:${Math.round(n / max * 100)}%"></i></span><span class="bar-num">${esc(n)}</span></div>`).join('') : '<div class="row-meta">No rejections in this window.</div>';
    const ag = Object.entries(d.agreement_by_violation || {});
    document.getElementById('q-agree').innerHTML = ag.length ? table(['violation', 'confirmed', 'rejected', 'inconclusive', 'agreement'], ag.map(([v, x]) => { const t = (x.confirmed || 0) + (x.rejected || 0) + (x.inconclusive || 0); return `<tr><td>${esc(vioLabel(v))}</td><td>${esc(x.confirmed || 0)}</td><td>${esc(x.rejected || 0)}</td><td>${esc(x.inconclusive || 0)}</td><td><b>${t ? Math.round((x.confirmed || 0) / t * 100) + '%' : '—'}</b></td></tr>`; })) : '<div class="row-meta">No decisions in this window.</div>';
  } catch (e) { document.getElementById('q-tiles').innerHTML = failBox(e, 'refresh'); }
};
PAGE_FN.escalations = async function(_, silent) {
  if (!silent) setPage(head('Escalations', 'Packages for the traffic authority wait for your approval. The fine happens on their side, never here.', `<select class="select" id="esc-status" style="width:auto"><option value="pending_approval">Pending approval</option><option value="approved">Approved</option><option value="dismissed">Dismissed</option><option value="">All</option></select><button class="btn btn-sm" type="button" data-action="refresh">Apply</button>`) + `<div id="esc-list"><div class="spinner"></div></div>`);
  const el = document.getElementById('esc-list'); if (!el) return;
  const st = document.getElementById('esc-status')?.value ?? 'pending_approval';
  try {
    const items = list(await api(`/admin/escalations${st ? '?status=' + st : ''}`));
    if (currentPage !== 'escalations') return;
    state.escalations = items;
    el.innerHTML = items.length ? table(['plate', 'threshold', 'status', 'created', ''], items.map(x => `<tr><td class="mono"><b>${esc(x.plate)}</b></td><td class="wrap">${esc(x.threshold_hit || '—')}</td><td>${chip(x.status)}</td><td>${esc(ago(x.created_at))}</td><td style="white-space:nowrap"><button class="btn btn-sm btn-ghost" type="button" data-action="esc-view" data-id="${esc(x.id)}">Package</button><button class="btn btn-sm btn-ghost" type="button" data-action="plate-lookup" data-plate="${esc(x.plate)}">History</button>${x.status === 'pending_approval' ? `<button class="btn btn-sm btn-green" type="button" data-action="esc-approve" data-id="${esc(x.id)}">Approve</button><button class="btn btn-sm" type="button" data-action="esc-dismiss" data-id="${esc(x.id)}">Dismiss</button>` : ''}</td></tr>`)) : empty('No escalations', 'Plates reach here after the confirmed-finding threshold.');
  } catch (e) { el.innerHTML = failBox(e, 'refresh'); }
};
function escView(id) {
  const x = (state.escalations || []).find(e => e.id === id); if (!x) return;
  const pk = x.package || {};
  openModal(`<h3>Package · <span class="mono">${esc(x.plate)}</span></h3><p class="sub">${esc(x.threshold_hit || '')} · ${chip(x.status)}</p>
    ${list(pk.cases).length ? table(['case', 'violation', 'decision', 'reviewer badge'], list(pk.cases).map(c => `<tr><td class="mono">${esc(short(c.case_id || c.id))}</td><td>${esc(vioLabel(c.violation))}</td><td>${esc(c.decision || 'confirmed')}</td><td>${esc(c.reviewer_badge || c.badge_number || '—')}</td></tr>`)) : ''}
    <details><summary class="row-meta" style="cursor:pointer">Raw package</summary><pre class="json-pre" style="margin-top:8px">${esc(JSON.stringify(pk, null, 2))}</pre></details>
    <div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Close</button></div>`);
}
async function escApprove(id) {
  if (!confirm('Approve this escalation? The package is built and the plate becomes "escalated".')) return;
  try { await api(`/admin/escalations/${encodeURIComponent(id)}/approve`, { method: 'POST' }); showToast('Escalation approved'); PAGE_FN.escalations(null, true); } catch (e) { showToast(e.message); }
}
function escDismiss(id) {
  openModal(`<h3>Dismiss escalation</h3><form id="m-form"><div class="field"><label>Reason (required)</label><textarea class="textarea" name="reason" minlength="5" required></textarea></div><div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Cancel</button><button class="btn btn-signal" type="submit">Dismiss</button></div></form>`);
  document.getElementById('m-form').onsubmit = async e => {
    e.preventDefault(); const reason = e.target.reason.value.trim();
    if (reason.length < 5) { showToast('Reason must be at least 5 characters'); return; }
    try { await api(`/admin/escalations/${encodeURIComponent(id)}/dismiss`, { method: 'POST', body: { reason } }); closeModal(); showToast('Escalation dismissed'); PAGE_FN.escalations(null, true); } catch (err) { showToast(err.message); }
  };
}
PAGE_FN.plates = function(plate) {
  setPage(head('Plate history', 'Two layers: unconfirmed AI observations and confirmed decisions. Only confirmed counts.') + `
    <form class="panel" id="plate-form" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><input class="input mono" name="plate" placeholder="MH12AB1234" autocapitalize="characters" maxlength="12" style="flex:1;min-width:160px" value="${esc(plate || '')}"><button class="btn btn-signal" type="submit">Look up</button></form><div id="plate-result"></div>`);
  document.getElementById('plate-form').addEventListener('submit', async e => {
    e.preventDefault();
    const p = e.target.plate.value.toUpperCase().replace(/[\s-]+/g, '');
    if (!PLATE_RE.test(p)) { showToast('Enter a valid Indian plate, e.g. MH12AB1234'); return; }
    const el = document.getElementById('plate-result'); el.innerHTML = '<div class="spinner"></div>';
    try { el.innerHTML = plateHistoryBlock(await api('/plates/' + encodeURIComponent(p) + '/history')); }
    catch (err) { el.innerHTML = err.status === 404 ? empty('Nothing on record', `${p} has never been observed.`) : failBox(err); }
  });
  if (plate) document.getElementById('plate-form').requestSubmit();
};
PAGE_FN.users = async function(_, silent) {
  if (!silent) setPage(head('Users', 'Approve reviewer requests after checking the badge. Role changes need a reason.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div class="panel"><div class="panel-title">Pending reviewer requests</div><div id="users-pending"><div class="spinner"></div></div></div><div class="panel"><div class="panel-title">All users</div><div id="users-all"></div></div>`);
  try {
    const rows = list(await api('/admin/users'));
    if (currentPage !== 'users') return;
    const pending = rows.filter(u => u.requested_role === 'officer' && !['officer', 'admin'].includes(u.role) && !u.deactivated_at);
    document.getElementById('users-pending').innerHTML = pending.length ? table(['user', 'badge number', 'requested', ''], pending.map(u => `<tr><td class="wrap"><b>${esc(u.full_name || '—')}</b><br><span style="color:var(--navy60)">${esc(u.email || '')}</span></td><td class="mono"><b>${esc(u.badge_number || '—')}</b></td><td>${esc(ago(u.role_requested_at || u.created_at))}</td><td style="white-space:nowrap"><button class="btn btn-sm btn-green" type="button" data-action="approve-reviewer" data-id="${esc(u.id)}" data-name="${esc(u.full_name || u.email || '')}">Approve</button></td></tr>`)) : '<div class="row-meta">No pending requests.</div>';
    document.getElementById('users-all').innerHTML = rows.length ? table(['user', 'role', 'badge', 'verified via', 'joined'], rows.map(u => `<tr class="${u.deactivated_at ? 'muted' : ''}">
      <td class="wrap"><b>${esc(u.full_name || '—')}</b><br><span style="color:var(--navy60)">${esc(u.email || '')}</span>${u.deactivated_at ? ' <span class="chip chip-red">deactivated</span>' : ''}</td>
      <td><select class="select" data-action="role-select" data-id="${esc(u.id)}" data-prev="${esc(u.role)}" ${u.id === uid() ? 'disabled title="You cannot change your own role"' : ''}>${['citizen', 'officer', 'admin'].map(r => `<option value="${r}" ${u.role === r ? 'selected' : ''}>${roleLabel(r)}</option>`).join('')}</select></td>
      <td class="mono">${esc(u.badge_number || '—')}</td><td class="wrap">${esc(u.verified_via || '—')}</td><td>${esc(ago(u.created_at))}</td></tr>`)) : empty('No users');
  } catch (e) { document.getElementById('users-pending').innerHTML = failBox(e, 'refresh'); }
};
function approveReviewer(id, name) {
  openModal(`<h3>Approve reviewer access</h3><p class="sub">${esc(name)} becomes a Reviewer. Record where the badge was verified.</p><form id="m-form"><div class="field"><label>Verified via (required)</label><input class="input" name="verified_via" minlength="3" required placeholder="e.g. Traffic HQ roster, phone call to station"></div><div class="field"><label>Reason</label><input class="input" name="reason" value="Reviewer request approved after badge check"></div><div class="modal-actions"><button class="btn" type="button" data-action="modal-close">Cancel</button><button class="btn btn-green" type="submit">Approve</button></div></form>`);
  document.getElementById('m-form').onsubmit = async e => {
    e.preventDefault(); const v = e.target.verified_via.value.trim(), reason = e.target.reason.value.trim();
    if (v.length < 3) { showToast('Say where the badge was verified'); return; }
    try { await api(`/admin/users/${encodeURIComponent(id)}/role`, { method: 'PATCH', body: { role: 'officer', reason: reason || 'Reviewer request approved', verified_via: v } }); closeModal(); showToast('Reviewer approved'); PAGE_FN.users(null, true); } catch (err) { showToast(err.message); }
  };
}
// Deactivate: no endpoint in API.md / routes/admin.py yet (profiles.deactivated_at exists, §9). Re-add the button once one is defined.
PAGE_FN.audit = async function() {
  setPage(head('Audit', 'Every admin and reviewer action, newest first.', `<button class="btn btn-sm" type="button" data-action="refresh">Refresh</button>`) + `<div id="audit-list"><div class="spinner"></div></div>`);
  try {
    const rows = list(await api('/admin/audit?limit=100'));
    if (currentPage !== 'audit') return;
    document.getElementById('audit-list').innerHTML = rows.length ? table(['when', 'actor', 'action', 'entity', 'reason'], rows.map(a => `<tr><td>${esc(ago(a.created_at))}</td><td>${esc(a.actor_role || '—')} ${esc(short(a.actor_id))}</td><td><b>${esc(a.action)}</b></td><td>${esc(a.entity)} <span class="mono">${esc(short(a.entity_id))}</span></td><td class="wrap">${esc(a.reason || '')}</td></tr>`)) : empty('No audit entries yet');
  } catch (e) { document.getElementById('audit-list').innerHTML = failBox(e, 'refresh'); }
};
PAGE_FN.settings = async function() {
  setPage(head('Settings', 'Thresholds, quota, retention and intake. Changes are audited.') + `<div id="settings-body"><div class="spinner"></div></div>`);
  try {
    const raw = await api('/admin/settings'), s = raw?.settings || raw || {};
    if (currentPage !== 'settings') return;
    const num = (k, label, hint, min = 0) => `<div class="field"><label for="s-${k}">${esc(label)}</label><input class="input" id="s-${k}" name="${k}" type="number" min="${min}" step="1" value="${esc(s[k] ?? '')}" required>${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>`;
    document.getElementById('settings-body').innerHTML = `<form class="panel" id="settings-form"><div class="two-col">
      ${num('max_uploads_per_day', 'Max uploads per user per day', 'Quota; 429 above it.', 1)}
      ${num('escalation_threshold_any', 'Escalation threshold — any tier', 'Confirmed findings of any kind before an escalation package is proposed.', 1)}
      ${num('escalation_threshold_top', 'Escalation threshold — top tier', 'Confirmed top-tier findings before escalation.', 1)}
      ${num('retention_days', 'Retention (days)', 'How long originals and evidence are kept.', 1)}
      </div><label class="check-row"><input type="checkbox" name="worker_paused" ${s.worker_paused ? 'checked' : ''}><span><b>Pause intake</b> — the worker stops claiming new clips.</span></label>
      <div class="modal-actions"><button class="btn btn-signal" type="submit">Save settings</button></div></form>`;
    document.getElementById('settings-form').addEventListener('submit', async e => {
      e.preventDefault(); const f = e.target;
      const body = { worker_paused: f.worker_paused.checked };
      for (const k of ['max_uploads_per_day', 'escalation_threshold_any', 'escalation_threshold_top', 'retention_days']) {
        const v = Number(f[k].value); if (!Number.isInteger(v) || v < 1) { showToast(`${k.replace(/_/g, ' ')} must be a whole number ≥ 1`); f[k].focus(); return; }
        body[k] = v;
      }
      if (!confirm('Save these settings? They take effect immediately.')) return;
      try { await api('/admin/settings', { method: 'PUT', body }); showToast('Settings saved'); } catch (err) { showToast(err.message); }
    });
  } catch (e) { document.getElementById('settings-body').innerHTML = failBox(e, 'refresh'); }
};
async function adminAction(label, fn) {
  try { await fn(); showToast(label); if (PAGE_FN[currentPage]) PAGE_FN[currentPage](currentId, true); }
  catch (e) { showToast(e.message); }
}

// ══════════════════════════════════════════════════════════
// ── Modal / toast ─────────────────────────────────────────
// ══════════════════════════════════════════════════════════
function openModal(html) {
  const body = document.getElementById('modal-body');
  if (body) body.innerHTML = html;
  document.getElementById('modal-backdrop')?.classList.add('open');
  setTimeout(() => body?.querySelector('textarea,input,select,button')?.focus(), 30);
}
function closeModal() {
  document.getElementById('modal-backdrop')?.classList.remove('open');
  const body = document.getElementById('modal-body'); if (body) body.innerHTML = '';
}
document.getElementById('modal-backdrop')?.addEventListener('click', e => { if (e.target.id === 'modal-backdrop') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); closeDrawer(); } });
let toastTimer;
window.showToast = function(msg) {
  const t = document.getElementById('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
};

// ══════════════════════════════════════════════════════════
// ── Click dispatcher (data-action) ────────────────────────
// ══════════════════════════════════════════════════════════
const ACTIONS = {
  'nav': el => { window.location.hash = '#' + el.dataset.page; },
  'open-sub': el => { window.location.hash = '#submission/' + el.dataset.id; },
  'open-case': el => { window.location.hash = '#case/' + el.dataset.id; },
  'drawer-open': openDrawer, 'drawer-close': closeDrawer, 'modal-close': closeModal,
  'refresh': () => { if (PAGE_FN[currentPage]) PAGE_FN[currentPage](currentId, true); },
  'queue-tab': el => { state.queueTab = el.dataset.tab; PAGE_FN.queue(null, true); },
  'alerts-filter': el => { state.alertsFilter = el.dataset.filter; renderAlertsList(); },
  'mark-all-read': () => markRead(state.notifications.map(n => n.id)),
  'notif-open': el => {
    const n = state.notifications.find(x => x.id === el.dataset.id); if (!n) return;
    markRead([n.id]); closeDrawer();
    const t = notifTarget(n); if (t) window.location.hash = t;
  },
  'enable-desktop': async () => {
    if (!('Notification' in window)) { showToast('This browser does not support desktop notifications'); return; }
    const p = await Notification.requestPermission();
    showToast(p === 'granted' ? 'Desktop alerts enabled' : 'Desktop alerts not enabled');
    renderAlertsList();
  },
  'withdraw': el => withdrawSub(el.dataset.id),
  'claim': el => claimCase(el.dataset.id),
  'release': el => releaseCase(el.dataset.id),
  'finalize': el => finalizeCase(el.dataset.id),
  'save-evidence': el => saveEvidence(el.dataset.fid),
  'reopen': el => reopenCase(el.dataset.id),
  'reassign': el => reassignCase(el.dataset.id),
  'wr-accept': el => answerWithdrawal(el.dataset.id, true),
  'wr-decline': el => answerWithdrawal(el.dataset.id, false),
  'esc-view': el => escView(el.dataset.id),
  'esc-approve': el => escApprove(el.dataset.id),
  'esc-dismiss': el => escDismiss(el.dataset.id),
  'plate-lookup': el => { window.location.hash = '#plates/' + el.dataset.plate; },
  'approve-reviewer': el => approveReviewer(el.dataset.id, el.dataset.name),
  'requeue': el => { if (confirm('Requeue this clip for processing?')) adminAction('Clip requeued', () => api(`/videos/${encodeURIComponent(el.dataset.id)}/requeue`, { method: 'POST' })); },
  'admin-pause': () => {
    const paused = !!state.admin?.paused;
    if (!confirm(paused ? 'Resume intake? The worker will start claiming clips again.' : 'Pause intake? The worker will stop claiming new clips.')) return;
    adminAction(paused ? 'Intake resumed' : 'Intake paused', () => api('/admin/queue/pause', { method: 'POST', body: { paused: !paused } }));
  },
  'admin-retry-failed': () => { if (confirm('Requeue ALL failed clips? Attempts are reset to 0.')) adminAction('Failed clips requeued', () => api('/admin/queue/retry-failed', { method: 'POST' })); },
};
document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el || el.tagName === 'SELECT') return;
  const fn = ACTIONS[el.dataset.action];
  if (fn) { e.preventDefault(); fn(el, e); }
});
document.addEventListener('change', async e => {
  const el = e.target.closest('[data-action="role-select"]');
  if (!el) return;
  const prev = el.dataset.prev, next = el.value;
  if (prev === next) return;
  const reason = prompt(`Change this user's role from ${roleLabel(prev)} to ${roleLabel(next)}. Reason (required):`);
  if (!reason || reason.trim().length < 3) { el.value = prev; if (reason !== null) showToast('A reason is required'); return; }
  const body = { role: next, reason: reason.trim() };
  if (next === 'officer') {
    const v = prompt('Where was the badge verified? (required to promote to Reviewer)');
    if (!v || v.trim().length < 3) { el.value = prev; showToast('Say where the badge was verified'); return; }
    body.verified_via = v.trim();
  }
  el.disabled = true;
  try { await api(`/admin/users/${encodeURIComponent(el.dataset.id)}/role`, { method: 'PATCH', body }); el.dataset.prev = next; showToast(`Role updated to ${roleLabel(next)}`); PAGE_FN.users(null, true); }
  catch (err) { el.value = prev; showToast(err.message); }
  finally { el.disabled = false; }
});

// ══════════════════════════════════════════════════════════
// ── Session Init + Auth State Listener (registered ONCE) ──
// ══════════════════════════════════════════════════════════
let _authListenerRegistered = false, _enterAppCalled = false;
function _safeEnterApp(user, profile) {
  if (_enterAppCalled) return;
  _enterAppCalled = true; hasActiveSession = true;
  updateUserUI(user, profile);
  if (splashEnded) enterApp(); // otherwise splashVideoEnded() enters once the splash fades
}
async function initAuthSession() {
  if (PREVIEW_USER) {
    const fakeUser = { id: '00000000-0000-0000-0000-000000000000', email: `preview-${PREVIEW_USER}@localhost`, user_metadata: {} };
    _safeEnterApp(fakeUser, { id: fakeUser.id, email: fakeUser.email, full_name: `Preview ${roleLabel(PREVIEW_USER)}`, role: PREVIEW_USER });
    return;
  }
  if (PREVIEW_ROLE === 'landing') return;
  const sb = getSB(); if (!sb) return;
  if (!_authListenerRegistered) {
    _authListenerRegistered = true;
    sb.auth.onAuthStateChange(async (event, session) => {
      if ((event === 'SIGNED_IN' || event === 'INITIAL_SESSION') && session?.user) {
        hasActiveSession = true;
        const profile = await fetchProfile(sb, session.user);
        _safeEnterApp(session.user, profile);
        if (window.location.hash.includes('access_token')) { window.history.replaceState(null, '', window.location.pathname + window.location.search + '#' + homePage()); route(); }
      } else if (event === 'SIGNED_OUT' || (event === 'INITIAL_SESSION' && !session)) {
        hasActiveSession = false; _enterAppCalled = false;
        updateUserUI(null, null);
        if (splashEnded) showLanding();
      }
    });
  }
  try {
    const { data: { session } } = await sb.auth.getSession();
    if (session?.user) { hasActiveSession = true; const profile = await fetchProfile(sb, session.user); _safeEnterApp(session.user, profile); }
  } catch (err) { console.log('Session check note:', err); }
}
window.addEventListener('DOMContentLoaded', initAuthSession);
