/**
 * API 1: register. POST <exec URL>?api=register   body {"email": "...", "github": "optional login", "team_key": "..."}
 * Answers {ok, email, github, token, devices, verified}. The token is shown once; only its hash is kept.
 * One developer (e-mail) can have up to USAGE.MAX_DEVICES machines. To reset someone, clear their "Token Hashes" cell.
 */
function registerApi_(b) {
  const email = usageEmail_(b.email);
  if (String(b.team_key || '') !== usageKey_()) throw usageErr_('wrong team key');
  if (!usageBump_('reg', USAGE.REGISTER_PER_HOUR)) throw usageErr_('too many registrations this hour, try later');
  let gh = String(b.github || '').trim();
  if (gh && !USAGE.GITHUB_RX.test(gh)) gh = '';

  const sh = usageSheet_(USAGE.USERS, USAGE.USER_HEAD);
  const row = usageFindUser_(sh, email);
  let hashes = [];
  let rec = null;
  if (row) {
    rec = sh.getRange(row, 1, 1, USAGE.USER_HEAD.length).getValues()[0];
    if (String(rec[5]).toLowerCase() === 'no') throw usageErr_('this user is switched off by an admin');
    hashes = String(rec[4] || '').split(',').filter(String);
    if (hashes.length >= USAGE.MAX_DEVICES) {
      throw usageErr_('already registered on ' + hashes.length + ' machines. Ask an admin to clear "Token Hashes" for ' + email + ' in the ' + USAGE.USERS + ' tab, then register again.');
    }
  }
  const verified = gh ? usageGithubExists_(gh) : '';      // 'Yes' | 'No' | 'Unchecked'; never blocks the registration
  const token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  hashes.push(usageHash_(token));
  if (row) {
    if (gh) {
      sh.getRange(row, 2).setValue(gh);
      sh.getRange(row, 4).setValue(verified);
    }
    sh.getRange(row, 5).setValue(hashes.join(','));
  } else {
    sh.appendRow([email, gh, new Date(), verified, hashes.join(','), 'Yes', '', 0, '']);
  }
  return { ok: true, email: email, github: gh || (rec ? String(rec[1] || '') : ''), token: token, devices: hashes.length, verified: verified === 'Yes' };
}

/** 'Yes' when GitHub knows the account, 'No' when it does not, 'Unchecked' when GitHub could not be asked (rate limit, outage). */
function usageGithubExists_(gh) {
  try {
    const r = UrlFetchApp.fetch('https://api.github.com/users/' + encodeURIComponent(gh), {
      muteHttpExceptions: true, headers: { 'Accept': 'application/vnd.github+json', 'User-Agent': 'cckit-usage-api' }
    });
    const code = r.getResponseCode();
    if (code === 200) return 'Yes';
    if (code === 404) return 'No';
  } catch (e) { /* fall through */ }
  return 'Unchecked';
}
