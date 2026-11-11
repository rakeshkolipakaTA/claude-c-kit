/**
 * API 2: usage. POST <exec URL>?api=usage   body: {"email", "token", "event_id", "trigger", "model", ...metrics}
 * Appends one row to Claude_Compactions for one context compaction, and updates the developer's Last Seen and counter.
 * Metrics (all optional numbers or short text): effort, claude_version, entrypoint, context_tokens, window, input_tokens, output_tokens,
 * cache_read, cache_write, requests, sub_requests, sub_output, tool_calls, models ("name:requests;..."), tools ("Bash:12;..."),
 * session_minutes, compactions_before, project, branch, session, os, partial, client.
 */
function usageApi_(b) {
  const email = usageEmail_(b.email);
  const sh = usageSheet_(USAGE.USERS, USAGE.USER_HEAD);
  const row = usageFindUser_(sh, email);
  if (!row) throw usageErr_('unknown user: register first');
  const rec = sh.getRange(row, 1, 1, USAGE.USER_HEAD.length).getValues()[0];
  const mine = usageHash_(String(b.token || ''));
  if (String(rec[4] || '').split(',').indexOf(mine) < 0) throw usageErr_('bad token');
  if (String(rec[5]).toLowerCase() === 'no') throw usageErr_('this user is switched off by an admin');
  if (!usageBump_('ev:' + email, USAGE.EVENTS_PER_HOUR)) throw usageErr_('rate limit');

  const id = usageStr_(b.event_id, 64);
  if (!id) throw usageErr_('event_id is required');
  const cache = CacheService.getScriptCache();
  if (cache.get('uid:' + id)) return { ok: true, duplicate: true };
  cache.put('uid:' + id, '1', 21600);

  const N = function (k, max) { return usageNum_(b[k], max || 1e12); };
  const ctx = N('context_tokens', 5e6), win = N('window', 5e6);
  const inp = N('input_tokens'), out = N('output_tokens'), cr = N('cache_read'), cw = N('cache_write');
  const allIn = inp + cr + cw;
  usageSheet_(USAGE.EVENTS, USAGE.EVENT_HEAD).appendRow([
    new Date(), id, email, String(rec[1] || ''), b.trigger === 'manual' ? 'manual' : 'auto',
    usageStr_(b.model, 60), usageStr_(b.effort, 20), usageStr_(b.claude_version, 20), usageStr_(b.entrypoint, 30),
    ctx, win, win > 0 ? Math.round(ctx * 1000 / win) / 10 : '',
    inp, out, cr, cw, allIn > 0 ? Math.round(cr * 1000 / allIn) / 10 : '',
    N('requests'), N('sub_requests'), N('sub_output'), N('tool_calls'),
    usageStr_(b.models, 300), usageStr_(b.tools, 300), N('session_minutes', 1e6), N('compactions_before', 1e5),
    usageStr_(b.project, 80), usageStr_(b.branch, 80), usageStr_(b.session, 64), usageStr_(b.os, 20),
    b.partial ? 'Yes' : '', usageStr_(b.client, 40)
  ]);
  sh.getRange(row, 7, 1, 2).setValues([[new Date(), (Number(rec[7]) || 0) + 1]]);
  return { ok: true };
}
