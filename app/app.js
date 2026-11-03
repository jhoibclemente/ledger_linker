const $ = (id) => document.getElementById(id);
let user = null, csrf = '', authMode = 'login', online = false, registrationOpen = true;
let limits = {max_files: 10, max_file_mb: 10}, selectedFiles = [], submissions = [], currentSubmission = null;
let toastTimer;

function message(text, error = false) {
  const box = $('notice');
  box.textContent = text; box.classList.toggle('error', error); box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { box.hidden = true; }, error ? 14000 : 6500);
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (csrf) headers['X-CSRF-Token'] = csrf;
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body);
  }
  let response;
  try { response = await fetch(`/api${path}`, {...options, headers, credentials: 'same-origin'}); }
  catch { throw new Error('Cannot reach the server. Check your connection and try again.'); }
  if (!(response.headers.get('content-type') || '').includes('application/json')) {
    throw new Error('Account services are not connected on this website yet. Open the Render application URL when deployment is complete.');
  }
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && user) showAuth();
    throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed. Check your input.');
  }
  return data;
}
async function busy(button, action, text = 'Working…') {
  const original = button.textContent;
  button.disabled = true; button.textContent = text;
  try { await action(); } catch (error) { message(error.message, true); }
  finally { button.textContent = original; button.disabled = false; }
}
function showAuth() {
  user = null; csrf = ''; currentSubmission = null; submissions = [];
  selectedFiles = []; $('file-list').replaceChildren(); $('review-rows').replaceChildren();
  $('search').value = ''; document.querySelector('.skip').href = '#main';
  $('original-files').replaceChildren(); $('submission-list').replaceChildren();
  $('upload-form').reset(); $('password-form').reset(); $('password').value = '';
  $('workspace-view').hidden = true; $('auth-view').hidden = false;
}
function mode(value) {
  authMode = value;
  const registering = value === 'register';
  $('name-field').hidden = !registering; $('name').required = registering;
  $('role-field').hidden = registering; $('password-help').hidden = !registering;
  $('password').minLength = registering ? 12 : 1;
  $('password').autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-title').textContent = registering ? 'A clearer start.' : 'Welcome back.';
  $('auth-subtitle').textContent = registering ? 'Create your personal statement workspace.' : 'Sign in to pick up where you left off.';
  $('auth-submit').textContent = registering ? 'Create account →' : 'Sign in →';
  $('auth-submit').disabled = !online || (registering && !registrationOpen);
  ['login', 'register'].forEach((name) => {
    $(`${name}-tab`).classList.toggle('selected', name === value);
    $(`${name}-tab`).setAttribute('aria-pressed', String(name === value));
  });
}
$('login-tab').addEventListener('click', () => mode('login'));
$('register-tab').addEventListener('click', () => mode('register'));
$('auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  await busy($('auth-submit'), async () => {
    const body = {email: $('email').value, password: $('password').value};
    if (authMode === 'register') {
      body.name = $('name').value;
      const result = await api('/register', {method: 'POST', body});
      mode('login'); $('password').value = ''; message(result.message); return;
    }
    const result = await api('/login', {method: 'POST', body});
    user = result.user; csrf = result.csrf;
    if ($('access').value === 'admin' && user.role !== 'admin') {
      await api('/logout', {method: 'POST'}); showAuth();
      throw new Error('This account has regular user access. Choose “Regular user” to sign in.');
    }
    $('password').value = ''; await enterWorkspace();
  }, authMode === 'register' ? 'Creating account…' : 'Signing in…');
  mode(authMode);
});
async function enterWorkspace() {
  const session = await api('/me');
  user = session.user; csrf = session.csrf; limits = session.settings;
  const admin = user.role === 'admin';
  $('auth-view').hidden = true; $('workspace-view').hidden = false;
  document.querySelector('.skip').href = '#workspace-main';
  $('user-label').textContent = user.name; $('role-badge').textContent = admin ? 'Administrator' : 'Regular user';
  $('settings-nav').hidden = !admin;
  $('history-nav-label').textContent = admin ? 'All submissions' : 'My submissions';
  $('history-title').textContent = admin ? 'All submissions' : 'My submissions';
  $('history-eyebrow').textContent = admin ? 'ADMINISTRATION' : 'YOUR RECORDS';
  $('history-description').textContent = admin ? 'Review and download submissions across all user accounts. Showing the latest 500.' : 'Your statements and exports, in one place. Showing the latest 500 submissions.';
  $('account-info').textContent = `${user.email} · ${admin ? 'Administrator' : 'Regular user'}`;
  updateLimitText(); await view(admin ? 'history' : 'upload');
}
function updateLimitText() {
  $('upload-limits').textContent = `Up to ${limits.max_files} PDFs · ${limits.max_file_mb} MB each · 50 MB total`;
}
$('signout').addEventListener('click', () => busy($('signout'), async () => {
  await api('/logout', {method: 'POST'}); showAuth(); message('You’re signed out.');
}));
async function view(name) {
  document.querySelectorAll('.workspace-section').forEach((section) => { section.hidden = section.id !== `${name}-view`; });
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  if (name === 'history') await loadHistory();
  if (name === 'settings') {
    const controls = [...$('settings-form').querySelectorAll('input, button')];
    controls.forEach((control) => { control.disabled = true; });
    $('settings-form').setAttribute('aria-busy', 'true');
    try {
      const data = await api('/admin/settings');
      $('registration-open').checked = data.registration_open;
      $('max-files').value = data.max_files; $('max-mb').value = data.max_file_mb;
    } finally {
      controls.forEach((control) => { control.disabled = false; });
      $('settings-form').removeAttribute('aria-busy');
    }
  }
  $('workspace-main').focus({preventScroll: true});
}
document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => view(button.dataset.view).catch((e) => message(e.message, true))));

function addFiles(files) {
  const incoming = Array.from(files);
  if (selectedFiles.length + incoming.length > limits.max_files) return message(`Choose at most ${limits.max_files} PDFs per submission.`, true);
  for (const file of incoming) {
    if (!file.name.toLowerCase().endsWith('.pdf')) return message('Only PDF files are accepted.', true);
    if (file.size > limits.max_file_mb * 1024 * 1024) return message(`${file.name} exceeds ${limits.max_file_mb} MB.`, true);
  }
  if ([...selectedFiles.map((f) => f.file), ...incoming].reduce((sum, f) => sum + f.size, 0) > 50 * 1024 * 1024) return message('The batch must be 50 MB or smaller.', true);
  incoming.forEach((file) => selectedFiles.push({file, bank: 'BPI', account: '', year: '', order: 'MDY'}));
  renderFiles(); $('pdf-files').value = '';
}
$('pdf-files').addEventListener('change', (event) => addFiles(event.target.files));
['dragenter', 'dragover'].forEach((name) => $('dropzone').addEventListener(name, (event) => {event.preventDefault(); $('dropzone').classList.add('dragover');}));
['dragleave', 'drop'].forEach((name) => $('dropzone').addEventListener(name, (event) => {event.preventDefault(); $('dropzone').classList.remove('dragover');}));
$('dropzone').addEventListener('drop', (event) => addFiles(event.dataTransfer.files));
function renderFiles() {
  $('file-list').replaceChildren();
  selectedFiles.forEach((item, index) => {
    const card = element('div', undefined, 'file-card'), header = element('header');
    const summary = element('div'); summary.append(element('strong', item.file.name), element('small', `${(item.file.size / 1024).toFixed(1)} KB`));
    const remove = element('button', 'Remove', 'quiet'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${item.file.name}`);
    remove.addEventListener('click', () => {selectedFiles.splice(index, 1); renderFiles();});
    header.append(summary, remove); card.append(header);
    const fields = element('div', undefined, 'file-fields');
    const definitions = [
      {key: 'bank', label: 'Bank', options: [['BPI','BPI'],['BDO','BDO'],['UnionBank','UnionBank']]},
      {key: 'account', label: 'Account number', placeholder: 'Auto-detect from PDF', help: 'Enter the exact number or mask shown.'},
      {key: 'year', label: 'Transaction year', type: 'number', placeholder: 'e.g. 2026', help: 'Required if dates omit the year.'},
      {key: 'order', label: 'Numeric date order', options: [['MDY','MM/DD · month first'],['DMY','DD/MM · day first']]},
    ];
    definitions.forEach((definition) => {
      const group = element('div'), id = `file-${index}-${definition.key}`, label = element('label', definition.label);
      label.htmlFor = id;
      const input = element(definition.options ? 'select' : 'input'); input.id = id;
      if (definition.options) definition.options.forEach(([value, text]) => { const option = element('option', text); option.value = value; input.append(option); });
      else {input.type = definition.type || 'text'; input.placeholder = definition.placeholder; input.maxLength = 40;}
      if (definition.key === 'year') {input.min = '1900'; input.max = '2100';}
      input.value = item[definition.key]; input.addEventListener('input', () => {item[definition.key] = input.value;});
      group.append(label, input); if (definition.help) group.append(element('small', definition.help)); fields.append(group);
    });
    card.append(fields); $('file-list').append(card);
  });
  $('convert').disabled = !selectedFiles.length;
}
$('upload-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  await busy($('convert'), async () => {
    if (!selectedFiles.length) throw new Error('Choose at least one PDF.');
    const data = new FormData(); data.append('title', $('batch-title').value);
    data.append('metadata', JSON.stringify(selectedFiles.map(({bank, account, year, order}) => ({bank, account, year, order}))));
    selectedFiles.forEach(({file}) => data.append('files', file));
    const result = await api('/submissions', {method: 'POST', body: data});
    selectedFiles = []; renderFiles(); $('upload-form').reset();
    await openSubmission(result.id); message('Statements extracted. Review each transaction before confirming.');
  }, 'Reading statements…');
  $('convert').disabled = !selectedFiles.length;
});
async function loadHistory() {
  $('submission-list').replaceChildren(element('p', 'Loading submissions…', 'muted'));
  submissions = await api('/submissions'); renderHistory();
}
$('refresh-history').addEventListener('click', () => busy($('refresh-history'), loadHistory, 'Refreshing…'));
$('search').addEventListener('input', renderHistory);
function renderHistory() {
  const list = $('submission-list'); list.replaceChildren();
  const query = $('search').value.toLowerCase();
  const filtered = submissions.filter((s) => `${s.title} ${s.owner}`.toLowerCase().includes(query));
  if (!filtered.length) {
    const empty = element('div', undefined, 'empty'); empty.append(element('h2', query ? 'No matching submissions' : 'A clean slate.'), element('p', query ? 'Try a different name or email address.' : 'Upload your first statement to start building your ledger.')); list.append(empty); return;
  }
  filtered.forEach((submission) => {
    const card = element('article', undefined, 'submission-card'), info = element('div'), title = element('h2', submission.title);
    const badge = element('span', submission.status === 'ready' ? 'Ready to download' : 'Needs review', `badge ${submission.status}`);
    info.append(title, badge, element('p', `${submission.file_count} PDFs · ${submission.row_count} transactions · ${new Date(submission.created_at * 1000).toLocaleString()}`));
    if (user.role === 'admin') info.append(element('p', `Submitted by ${submission.owner}`));
    const actions = element('div', undefined, 'submission-actions'), open = element('button', submission.status === 'draft' ? 'Review →' : 'View', 'secondary');
    open.addEventListener('click', () => busy(open, () => openSubmission(submission.id)));
    actions.append(open);
    if (submission.status === 'ready') {const link = element('a', 'Download CSV ↓', 'button primary'); link.href = `/api/submissions/${submission.id}/csv`; actions.append(link);}
    const remove = element('button', 'Delete', 'quiet'); remove.addEventListener('click', () => deleteSubmission(submission.id)); actions.append(remove);
    card.append(info, actions); list.append(card);
  });
}
async function openSubmission(id) {
  currentSubmission = await api(`/submissions/${id}`);
  const draft = currentSubmission.status === 'draft';
  await view('review');
  $('review-title').textContent = currentSubmission.title;
  $('review-description').textContent = draft ? 'Correct any extracted values below, then confirm the full batch.' : 'Finalized transactions. Download your aggregated CSV or original PDFs below.';
  $('review-count').textContent = `${currentSubmission.records.length} transactions · ${currentSubmission.file_count} files`;
  $('review-rows').replaceChildren();
  currentSubmission.records.forEach((row, index) => {
    const tr = element('tr'), source = element('td');
    source.append(element('strong', row.bank), element('small', row.account_number, 'account-number')); tr.append(source);
    const file = element('td'); file.append(element('span', row.source_file, 'source')); tr.append(file);
    ['transaction_date','description','debit','credit','balance'].forEach((key) => {
      const cell = element('td');
      if (draft) {
        const input = element('input'); input.type = key === 'transaction_date' ? 'date' : 'text'; input.value = row[key];
        input.setAttribute('aria-label', `${key.replaceAll('_',' ')} for transaction ${index + 1}`);
        if (key === 'description') {input.className = 'description'; input.maxLength = 2000;}
        else if (key !== 'transaction_date') {input.inputMode = 'decimal'; input.maxLength = 30;}
        input.addEventListener('input', () => {row[key] = input.value; $('review-check').checked = false; $('confirm').disabled = true;}); cell.append(input);
      } else cell.textContent = row[key];
      tr.append(cell);
    });
    $('review-rows').append(tr);
  });
  $('review-check').checked = false; $('confirm').disabled = true;
  $('confirm').hidden = !draft; $('review-check-label').hidden = !draft; $('download-ready').hidden = draft;
  $('download-ready').href = `/api/submissions/${id}/csv`;
  $('original-files').replaceChildren(element('strong', 'Original statements:'));
  currentSubmission.files.forEach((file) => {const link = element('a', `${file.filename} ↓`); link.href = `/api/submissions/${id}/files/${file.id}`; $('original-files').append(link);});
}
$('review-check').addEventListener('change', () => {$('confirm').disabled = !$('review-check').checked;});
$('confirm').addEventListener('click', () => busy($('confirm'), async () => {
  if (!$('review-check').checked || !currentSubmission) return;
  const id = currentSubmission.id;
  await api(`/submissions/${id}/confirm`, {method: 'POST', body: {records: currentSubmission.records}});
  await openSubmission(id); message('Your aggregated CSV is ready to download.');
}, 'Creating CSV…'));
$('back-history').addEventListener('click', () => view('history').catch((e) => message(e.message, true)));
async function deleteSubmission(id) {
  const dialog = $('delete-dialog'); dialog.returnValue = ''; dialog.showModal();
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'delete') return;
    try {await api(`/submissions/${id}`, {method: 'DELETE'}); await loadHistory(); message('Submission and original files deleted.');}
    catch (error) {message(error.message, true);}
  }, {once: true});
}
$('settings-form').addEventListener('submit', async (event) => {
  event.preventDefault(); await busy(event.submitter, async () => {
    limits = await api('/admin/settings', {method: 'PUT', body: {registration_open: $('registration-open').checked, max_files: Number($('max-files').value), max_file_mb: Number($('max-mb').value)}});
    updateLimitText(); message('Workspace settings saved.');
  }, 'Saving…');
});
$('password-form').addEventListener('submit', async (event) => {
  event.preventDefault(); await busy(event.submitter, async () => {
    const result = await api('/account/password', {method: 'POST', body: {current_password: $('current-password').value, new_password: $('new-password').value}});
    showAuth(); message(result.message);
  }, 'Updating…');
});
async function start() {
  try {
    const health = await api('/health'); online = health.status === 'ok'; registrationOpen = health.registration_open;
    $('connection').textContent = registrationOpen ? 'Account services are connected.' : 'Sign in to your account. New registrations are currently closed.';
    $('register-tab').disabled = !registrationOpen; mode('login');
    try {await enterWorkspace();} catch (error) {if (error.message !== 'Please sign in.') message(error.message, true);}
  } catch (error) {
    $('connection').textContent = error.message; $('connection').classList.add('error');
    $('auth-submit').disabled = true;
  }
}
start();
