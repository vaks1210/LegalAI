/*
 * Stored-XSS guard. Both portals build their UI with template literals and
 * innerHTML, interpolating text written by the *other* party (dispute text,
 * counter-claims, proof requests, explanations). Escaping every string in
 * API responses once, here, keeps all current and future templates safe.
 *
 * Auth responses are skipped: they only contain the user's own profile,
 * which is written into <input>.value where entities would show literally.
 */
(function () {
  var MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeString(s) { return s.replace(/[&<>"']/g, function (c) { return MAP[c]; }); }
  function escapeDeep(v) {
    if (typeof v === 'string') return escapeString(v);
    if (Array.isArray(v)) return v.map(escapeDeep);
    if (v && typeof v === 'object') {
      var out = {};
      Object.keys(v).forEach(function (k) { out[k] = escapeDeep(v[k]); });
      return out;
    }
    return v;
  }
  window.escapeHtml = escapeString;
  if (window.axios) {
    window.axios.interceptors.response.use(function (response) {
      var url = (response.config && response.config.url) || '';
      if (url.indexOf('/api/auth/') === -1) response.data = escapeDeep(response.data);
      return response;
    });
  }
})();
