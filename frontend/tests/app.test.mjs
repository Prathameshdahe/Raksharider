// Runnable check for the frontend payload-shape fixes. No framework: `node frontend/tests/app.test.mjs`.
// app.js is a plain script, so it is evaluated in a vm context with a minimal DOM stub; its top-level
// functions then live on that context and can be asserted directly.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '..', 'app.js'), 'utf8');

const noop = () => {};
const el = { classList: { add: noop, remove: noop, toggle: noop, contains: () => false }, style: {}, addEventListener: noop };
const ctx = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, URLSearchParams, Date, Math, JSON, performance,
  window: { location: { hostname: 'localhost', search: '', hash: '', pathname: '/' }, addEventListener: noop, scrollTo: noop, innerWidth: 800, innerHeight: 600 },
  document: { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, body: el },
  localStorage: { length: 0, key: () => null, getItem: () => null },
  navigator: { userAgent: 'node' },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const { plateOf, plateHistoryBlock, vtLabel } = ctx;

// ── F4 / F3: the case-detail payload resolves its plate from ai.resolved_plate + plate_of_record ──
const detail = {
  id: 'c1', track_id: 7,
  ai: { resolved_plate: { text: 'MH12AB1234', confidence: 0.81 }, track: { vehicle_type: 'two_wheeler' } },
  plate_of_record: 'MH12AB1234',
  uploader_claim: { claimed_plate: 'MH12AB1234', vehicle_type: 'two_wheeler' },
};
assert.equal(plateOf(detail).ai, 'MH12AB1234', 'F4: ai_value for a plate correction must be the AI plate, not null');
assert.equal(plateOf(detail).text, 'MH12AB1234');
assert.equal(plateOf(detail).conf, 0.81);
assert.equal(plateOf(detail).corrected, null, 'plate_of_record equal to the AI read is not a correction');
// once a reviewer corrects it, plate_of_record diverges and becomes the corrected value
const corrected = { ...detail, plate_of_record: 'MH12AB1284' };
assert.equal(plateOf(corrected).corrected, 'MH12AB1284');
assert.equal(plateOf(corrected).ai, 'MH12AB1234', 'a correction must not overwrite the AI value sent as ai_value');
assert.equal(plateOf(corrected).text, 'MH12AB1284');

// ── list payload keeps working ──
const listRow = { id: 'c1', plate: { ai: 'DL01CD5678', confidence: 0.66, claimed: 'DL01CD5673', corrected: null } };
assert.equal(plateOf(listRow).ai, 'DL01CD5678');
assert.equal(plateOf(listRow).claimed, 'DL01CD5673');
// and a payload with no plate at all renders nothing, never "undefined"
assert.equal(plateOf({ id: 'c1' }).text, null);
assert.equal(vtLabel(undefined), 'unknown', 'F3: a missing vehicle_type must never print "undefined"');

// ── F2: plate history in both shapes ──
const flat = [
  { layer: 'confirmed', plate: 'MH12AB1234', violation: 'no_helmet', case_id: 'c5', created_at: new Date().toISOString() },
  { layer: 'observed', plate: 'MH12AB1234', violation: 'phone_usage', case_id: 'c1', created_at: new Date().toISOString() },
];
const fromArray = plateHistoryBlock(flat);
assert.match(fromArray, /1 confirmed/, 'F2: a flat array must be bucketed by row.layer');
assert.match(fromArray, /1 unconfirmed/);
assert.match(fromArray, /MH12AB1234/);
const fromObject = plateHistoryBlock({ confirmed: [flat[0]], observed: [flat[1]] }, 'MH12AB1234');
assert.match(fromObject, /1 confirmed/, 'F2: the {confirmed, observed} shape must still render');
assert.match(fromObject, /MH12AB1234/, 'the plate falls back to the case plate when the payload omits it');
assert.doesNotMatch(plateHistoryBlock({ confirmed: [], observed: [] }, 'MH12AB1234'), /undefined/);
// GET /cases/{id} sends no `status`; claiming "Clean" beside confirmed rows would be false.
assert.doesNotMatch(plateHistoryBlock({ confirmed: [flat[0]], observed: [] }, 'MH12AB1234'), /Clean</, 'no status in the payload → no status chip');
// /plates/{p}/history does send one, and it must still show.
assert.match(plateHistoryBlock({ plate: 'MH12AB1234', status: 'watch', confirmed: [flat[0]], observed: [] }), /Watch/);

// ── F9: nothing in the citizen submission view renders an evidence frame ──
const citizenView = src.slice(src.indexOf('PAGE_FN.submission ='), src.indexOf('async function withdrawSub'));
assert.doesNotMatch(citizenView, /framesStrip|\.evidence/, 'F9: citizens receive no evidence JPEGs');
assert.match(citizenView, /detection_video_url/, 'F9: citizens receive the detection video instead');
assert.match(citizenView, /findingRow/, 'F9: citizens receive the textual outcome per finding');
assert.doesNotMatch(src, /function framesStrip/, 'F9: the evidence strip is gone, not merely unused');

// ── F5: the pending-reviewer table reads profiles.role_requested_at ──
assert.doesNotMatch(src, /u\.requested_at/, 'F5: profiles has role_requested_at, not requested_at');
assert.match(src, /u\.role_requested_at/);

// ── F6 / F7: withdrawal reason kept, finalized cases attributed to finalized_by first ──
assert.match(src, /\/withdraw`, \{ method: 'POST', body: \{ reason \} \}/, 'F6: the withdrawal request still carries its reason');
assert.match(src, /c\.finalized_by \|\| c\.claimed_by/, 'F7: prefer finalized_by when the payload has it');

// ── F8: the withdrawal answer posts the body routes/cases.py expects ──
assert.match(src, /post\(\{ accept: true \}\)/, 'F8: accept posts {accept: true}');
assert.match(src, /post\(\{ accept: false, reason \}\)/, 'F8: decline posts {accept: false, reason}');
assert.match(src, /'wr-accept'|'wr-decline'/, 'F8: both buttons are wired to the click dispatcher');

// ── Auth errors are keyed on GoTrue error codes; message text is only a fallback ──
const { authErrorMessage, authErrorFromUrl } = ctx;
assert.equal(authErrorMessage({ code: 'email_not_confirmed', message: 'Email not confirmed', status: 400 }).action, 'resend');
assert.equal(authErrorMessage({ message: 'Email not confirmed' }).code, 'email_not_confirmed', 'a response without a code still classifies');
const bad = authErrorMessage({ code: 'invalid_credentials', message: 'Invalid login credentials', status: 400 });
assert.equal(bad.code, 'invalid_credentials');
assert.equal(bad.action, 'forgot');
assert.match(bad.text, /Google/, 'a Google-created account has no password and gets the same 400');
assert.equal(authErrorMessage({ code: 'over_email_send_rate_limit', message: 'For security purposes, you can only request this after 42 seconds.', status: 429 }).code, 'over_email_send_rate_limit');
assert.equal(authErrorMessage({ status: 429, message: 'Request rate limit reached' }).code, 'over_request_rate_limit');
assert.match(authErrorMessage({ message: 'Email address "x@y.z" is not authorized' }).text, /SMTP/, 'the built-in mailer refusal points at the owner');
assert.equal(authErrorMessage({ status: 0, message: 'Failed to fetch' }).code, 'network');
assert.match(authErrorMessage({ status: 0, message: 'Failed to fetch' }).text, /\/reset/);
assert.equal(authErrorMessage({ code: 'weak_password', message: 'Password should be at least 6 characters.' }).text, 'Password should be at least 6 characters.');
assert.equal(authErrorMessage({ code: 'same_password', message: 'New password should be different from the old password.' }).code, 'same_password');
assert.equal(authErrorMessage({ code: 'user_banned', message: 'User is banned' }).action, null);
// (objects born inside the vm context carry its own Object prototype, so compare by value)
assert.deepEqual(JSON.parse(JSON.stringify(authErrorFromUrl('#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired', ''))),
  { error: 'access_denied', code: 'otp_expired', message: 'Email link is invalid or has expired' });
assert.equal(authErrorFromUrl('#access_token=abc&type=recovery', ''), null);
assert.equal(authErrorFromUrl('#queue', ''), null, 'ordinary hash routes are not errors');
assert.equal(authErrorFromUrl('', '?error=server_error&error_description=Unable+to+exchange').message, 'Unable to exchange');
// every e-mail / OAuth link returns to the site root: never pathname (index.html#admin) and never /reset
assert.doesNotMatch(src, /\$\{window\.location\.origin\}\$\{window\.location\.pathname\}/, 'auth links must land on the site root');
assert.match(src, /signInWithOAuth\(\{ provider: 'google', options: \{ redirectTo: authReturnUrl\(\) \} \}\)/);
assert.match(src, /resetPasswordForEmail\(email, \{ redirectTo: authReturnUrl\(\) \}\)/);
assert.match(src, /signUp\(\{\s*email, password,\s*options: \{ emailRedirectTo: authReturnUrl\(\)/);
// a failed login never sends mail (built-in mailer: 2 e-mails/hour for the whole project)
const explainBody = src.slice(src.indexOf('function explainAuthError'), src.indexOf('window.doResendConfirmation'));
assert.doesNotMatch(explainBody, /auth\.resend|resetPasswordForEmail/, 'no mail as a side effect of a failed login');
// the recovery dialog is opened by the shell, never by a timer racing it, and survives the first route()
assert.doesNotMatch(src, /setTimeout\(openPasswordReset/, 'a timer raced the shell and route() destroyed the hidden dialog');
assert.match(src, /route\(\);\s*if \(pendingPasswordReset\) openPasswordReset\(\);/, 'initApp opens the pending dialog after the first route()');
assert.match(src, /if \(keepModalOnRoute\) keepModalOnRoute = false; else closeModal\(\);/, 'route() keeps the freshly opened recovery dialog once');
// the profile read tolerates a missing row; legacy pre-002 rows never print a raw status
assert.match(src, /\.eq\('id', user\.id\)\.maybeSingle\(\)/);
assert.match(src, /completed: 'decided'/);
// an existing address re-registering is told so (GoTrue returns a user with no identities)
assert.match(src, /data\.user\.identities\.length === 0/);

console.log('frontend/app.js — all checks passed');
