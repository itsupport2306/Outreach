// Global variables
var allResults = [];
var filteredResults = [];
var currentPage = 1;
var perPage = parseInt(localStorage.getItem('perPage') || '20', 10);
var totalResults = 0;
var totalPages = 1;
var isLoading = false;
var syncInitialized = false;
var currentView = 'home';
var sheetOutreachCampaignId = null;
var sheetOutreachColumnsReady = false;
var sheetOutreachSending = false;
var sheetOutreachRecipientCount = 0;
var sheetOutreachLiveEnabled = false;

// UI helpers
var toastHost;

// Reveal animations
function wrapChars(el) {
  const text = el.textContent;
  el.innerHTML = '';
  for (let i = 0; i < text.length; i++) {
    const span = document.createElement('span');
    span.className = 'char';
    span.textContent = text[i] === ' ' ? '\u00A0' : text[i];
    el.appendChild(span);
  }
}

function _formatIso(iso) {
  try {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  } catch (e) {
    return String(iso || '');
  }
}

function _pillClassForStatus(st) {
  const s = String(st || '').toLowerCase();
  if (s === 'completed' || s === 'sent') return 'ok';
  if (s === 'running' || s === 'queued') return 'warn';
  if (s === 'failed') return '';
  if (s === 'dry_run') return 'warn';
  if (s === 'blocked') return '';
  return '';
}

async function loadCeipalDashboard() {
  if (!elements.ceipalSection) return;
  try {
    const [jobsRes, batchesRes] = await Promise.all([
      fetch('/api/ceipal/status?limit=50'),
      fetch('/api/ceipal/batches?limit=10'),
    ]);
    const jobsData = jobsRes.ok ? await jobsRes.json() : { jobs: [] };
    const batchesData = batchesRes.ok ? await batchesRes.json() : { batches: [] };
    _ceipalState.jobs = Array.isArray(jobsData.jobs) ? jobsData.jobs : [];
    _ceipalState.batches = Array.isArray(batchesData.batches) ? batchesData.batches : [];

    renderCeipalJobsTable();
    syncCeipalKpis();

    // If a job is already selected, refresh its detail
    if (_ceipalState.activeJobId) {
      loadCeipalJobDetail(_ceipalState.activeJobId);
    }
  } catch (e) {
    console.error('Failed to load CEIPAL dashboard:', e);
  }
}

function syncCeipalKpis() {
  try {
    const lastBatch = (_ceipalState.batches || [])[0];
    if (!lastBatch) {
      if (elements.ceipalKpiJobs) elements.ceipalKpiJobs.textContent = '0';
      if (elements.ceipalKpiSelected) elements.ceipalKpiSelected.textContent = '0';
      if (elements.ceipalKpiSent) elements.ceipalKpiSent.textContent = '0';
      return;
    }
    if (elements.ceipalKpiJobs) elements.ceipalKpiJobs.textContent = String(lastBatch.jobs_processed || 0);
    if (elements.ceipalKpiSelected) elements.ceipalKpiSelected.textContent = String(lastBatch.candidates_selected || 0);
    if (elements.ceipalKpiSent) elements.ceipalKpiSent.textContent = String(lastBatch.messages_sent || 0);
  } catch (e) {}
}

function renderCeipalJobsTable() {
  if (!elements.ceipalJobsTable) return;
  const tbody = elements.ceipalJobsTable;
  tbody.innerHTML = '';
  const jobs = Array.isArray(_ceipalState.jobs) ? _ceipalState.jobs : [];
  if (!jobs.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" class="muted">No ATS processing runs found yet. Run /api/ceipal/process to create runs.</td>';
    tbody.appendChild(tr);
    return;
  }

  jobs.forEach((j) => {
    const id = j.id;
    const jobTitle = (j.job_title || j.job_code || 'Job').toString();
    const jobCode = (j.job_code || '').toString();
    const status = (j.status || '').toString();
    const selected = j.selected_candidates || 0;
    const started = _formatIso(j.started_at);
    const finished = _formatIso(j.finished_at);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(jobTitle)}<div class="muted">${escapeHtml(jobCode)}</div></td>
      <td><span class="pill ${_pillClassForStatus(status)}">${escapeHtml(status || 'unknown')}</span></td>
      <td>${escapeHtml(String(selected))}</td>
      <td>${escapeHtml(started)}</td>
      <td>${escapeHtml(finished)}</td>
      <td><button class="btn btn-secondary btn-sm" type="button" data-ceipal-job="${escapeHtml(String(id))}">View</button></td>
    `;
    const btn = tr.querySelector('button[data-ceipal-job]');
    if (btn) {
      btn.addEventListener('click', () => loadCeipalJobDetail(id));
    }
    tbody.appendChild(tr);
  });
}

async function loadCeipalJobDetail(jobRunId) {
  if (!elements.ceipalCandidatesTable || !elements.ceipalCandidatesWrap) return;
  try {
    _ceipalState.activeJobId = jobRunId;
    if (elements.ceipalDetailHint) elements.ceipalDetailHint.hidden = true;
    elements.ceipalCandidatesWrap.hidden = true;

    const res = await fetch(`/api/ceipal/status/${encodeURIComponent(String(jobRunId))}?limit=500`);
    const data = res.ok ? await res.json() : null;
    const candidates = data && Array.isArray(data.candidates) ? data.candidates : [];
    _ceipalState.activeCandidates = candidates;

    renderCeipalCandidatesTable(candidates);
    elements.ceipalCandidatesWrap.hidden = false;
  } catch (e) {
    console.error('Failed to load CEIPAL job detail:', e);
    if (elements.ceipalDetailHint) {
      elements.ceipalDetailHint.hidden = false;
      elements.ceipalDetailHint.textContent = 'Failed to load candidate statuses for this job run.';
    }
  }
}

function renderCeipalCandidatesTable(candidates) {
  const tbody = elements.ceipalCandidatesTable;
  if (!tbody) return;
  tbody.innerHTML = '';

  const items = Array.isArray(candidates) ? candidates : [];
  if (!items.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" class="muted">No selected candidates recorded for this job run.</td>';
    tbody.appendChild(tr);
    return;
  }

  items.forEach((c) => {
    const name = c.candidate_name || c.candidate_id || 'Candidate';
    const contact = [c.candidate_phone, c.candidate_email].filter(Boolean).join(' • ');
    const score = c.score || '';
    const sms = c.sms_status || '';
    const email = c.email_status || '';
    const status = c.current_status || '';
    const isFinal = !!c.final_selected;
    const latestResultId = c.latest_interview_result_id;
    const created = _formatIso(c.created_at);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(String(name))}<div class="muted">${escapeHtml(String(contact || ''))}</div></td>
      <td>${escapeHtml(String(score))}</td>
      <td><span class="pill ${_pillClassForStatus(sms)}">${escapeHtml(String(sms))}</span></td>
      <td><span class="pill ${_pillClassForStatus(email)}">${escapeHtml(String(email))}</span></td>
      <td><span class="pill ${_pillClassForStatus(status)}">${escapeHtml(String(status || ''))}</span></td>
      <td>
        ${isFinal
          ? `<span class="pill ok">final</span>`
          : `<button class="btn btn-secondary btn-sm" type="button" data-ats-final="1">Mark</button>`}
      </td>
      <td>
        ${latestResultId
          ? `<button class="btn btn-secondary btn-sm" type="button" data-ats-transcript="${escapeHtml(String(latestResultId))}">View</button>`
          : `<span class="muted">—</span>`}
      </td>
      <td>
        ${latestResultId
          ? `<button class="btn btn-secondary btn-sm" type="button" data-ats-messages="${escapeHtml(String(latestResultId))}">View</button>`
          : `<span class="muted">—</span>`}
      </td>
      <td>${escapeHtml(String(created))}</td>
    `;

    const markBtn = tr.querySelector('button[data-ats-final]');
    if (markBtn) {
      markBtn.addEventListener('click', () => {
        openAtsFinalModal({
          candidate_id: c.candidate_id || '',
          email: c.candidate_email || '',
          phone: c.candidate_phone || '',
          notes: ''
        });
      });
    }

    const tBtn = tr.querySelector('button[data-ats-transcript]');
    if (tBtn) {
      const rid = tBtn.getAttribute('data-ats-transcript');
      tBtn.addEventListener('click', () => {
        if (rid) showInterviewDetail(rid);
      });
    }

    const mBtn = tr.querySelector('button[data-ats-messages]');
    if (mBtn) {
      const rid = mBtn.getAttribute('data-ats-messages');
      mBtn.addEventListener('click', async () => {
        if (!rid) return;
        await showInterviewDetail(rid);
        try { setDetailTab('messages'); } catch (e) {}
      });
    }

    tbody.appendChild(tr);
  });
}

function openAtsFinalModal(prefill) {
  if (!elements.atsFinalModal) return;
  if (elements.atsFinalCandidateId) elements.atsFinalCandidateId.value = (prefill && prefill.candidate_id) ? String(prefill.candidate_id) : '';
  if (elements.atsFinalEmail) elements.atsFinalEmail.value = (prefill && prefill.email) ? String(prefill.email) : '';
  if (elements.atsFinalPhone) elements.atsFinalPhone.value = (prefill && prefill.phone) ? String(prefill.phone) : '';
  if (elements.atsFinalNotes) elements.atsFinalNotes.value = (prefill && prefill.notes) ? String(prefill.notes) : '';
  if (elements.atsFinalStatus) elements.atsFinalStatus.textContent = '';
  elements.atsFinalModal.hidden = false;
}

function closeAtsFinalModal() {
  if (!elements.atsFinalModal) return;
  elements.atsFinalModal.hidden = true;
}

async function saveAtsFinalSelection() {
  try {
    if (!elements.saveAtsFinalBtn) return;
    if (elements.atsFinalStatus) elements.atsFinalStatus.textContent = '';

    const payload = {
      candidate_id: (elements.atsFinalCandidateId ? elements.atsFinalCandidateId.value : '').trim() || null,
      email: (elements.atsFinalEmail ? elements.atsFinalEmail.value : '').trim() || null,
      phone: (elements.atsFinalPhone ? elements.atsFinalPhone.value : '').trim() || null,
      notes: (elements.atsFinalNotes ? elements.atsFinalNotes.value : '').trim() || null
    };

    const res = await fetch('/api/ats/final', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(await res.text());

    if (elements.atsFinalStatus) {
      elements.atsFinalStatus.textContent = 'Saved.';
    }
    closeAtsFinalModal();
    // Refresh ATS table state
    await loadCeipalDashboard();
  } catch (e) {
    console.error('Failed to save final selected candidate:', e);
    if (elements.atsFinalStatus) elements.atsFinalStatus.textContent = 'Failed to save. Please try again.';
  }
}

function initRevealAnimations() {
  // Character-by-character reveals
  const charElements = document.querySelectorAll('.reveal-chars');
  charElements.forEach(el => {
    wrapChars(el);
    const delay = parseInt(el.dataset.revealDelay || 0, 10);
    setTimeout(() => {
      el.classList.add('revealed');
    }, delay);
  });

  // Staggered block reveals
  const staggerElements = document.querySelectorAll('.reveal-stagger');
  staggerElements.forEach(el => {
    const delay = parseInt(el.dataset.revealDelay || 0, 10);
    setTimeout(() => {
      el.classList.add('revealed');
    }, delay);
  });
}

// Dashboard/analytics state
var _dashboardState = { schedules: [], results: [], sms: [] };
var _analyticsState = { results: [], filtered: [] };
var _ceipalState = { jobs: [], batches: [], activeJobId: null, activeCandidates: [] };

// DOM Elements
var elements = {};
var loadingOverlay;
var rolePreset;
var jdTextarea;
var jdCounter;
var useExampleBtn;
// Global cache of original candidates loaded from the backend
window.originalCandidatesData = window.originalCandidatesData || [];

// Initialize DOM elements
function initializeElements() {
  const el = (id) => document.getElementById(id);
  
  // Store elements in the elements object
  elements = {
    results: el('results'),
    resultsPanel: el('resultsPanel'),
    resultsTable: el('resultsTable') ? el('resultsTable').querySelector('tbody') : null,
    statusEl: el('status'),
    filterInput: el('filterInput'),
    pageSize: el('pageSize'),
    currentPage: el('currentPage'),
    totalPages: el('totalPages'),
    resultCount: el('resultCount'),
    resultRange: el('resultRange'),
    totalResultsEl: el('totalResults'),
    firstPageBtn: el('firstPage'),
    prevPageBtn: el('prevPage'),
    nextPageBtn: el('nextPage'),
    lastPageBtn: el('lastPage'),
    selectAll: el('selectAll'),
    rankBtn: el('rankBtn'),
    addBestBtn: el('addBestBtn'),
    viewBestBtn: el('viewBestBtn'),
    quickSendBtn: el('quickSendBtn'),
    sendMessageBtn: el('sendMessageBtn'),
    emailBtn: el('emailBtn'),
    smsModal: el('smsModal'),
    smsPhone: el('smsPhone'),
    smsMessage: el('smsMessage'),
    sendSmsBtn: el('sendSmsBtn'),
    closeSmsModal: el('closeSmsModal'),
    cancelSmsBtn: el('cancelSmsBtn'),
    availabilityModal: el('availabilityModal'),
    availabilityTable: el('availabilityTable') ? el('availabilityTable').querySelector('tbody') : null,
    viewAvailabilityBtn: el('viewAvailabilityBtn'),
    closeAvailabilityModal: el('closeAvailabilityModal'),
    viewDashboardBtn: el('viewDashboardBtn'),
    dashboardPanel: el('dashboardPanel'),
    navHome: el('navHome'),
    navInterviews: el('navInterviews'),
    navAnalytics: el('navAnalytics'),
    navATS: el('navATS'),
    viewHome: el('viewHome'),
    analyticsPanel: el('analyticsPanel'),
    atsPanel: el('atsPanel'),
    ceipalSection: el('ceipalSection'),
    ceipalRefresh: el('ceipalRefresh'),
    ceipalKpiJobs: el('ceipalKpiJobs'),
    ceipalKpiSelected: el('ceipalKpiSelected'),
    ceipalKpiSent: el('ceipalKpiSent'),
    ceipalJobsTable: el('ceipalJobsTable') ? el('ceipalJobsTable').querySelector('tbody') : null,
    ceipalCandidatesWrap: el('ceipalCandidatesWrap'),
    ceipalCandidatesTable: el('ceipalCandidatesTable') ? el('ceipalCandidatesTable').querySelector('tbody') : null,
    ceipalDetailHint: el('ceipalDetailHint'),
    goAnalyticsBtn: el('goAnalyticsBtn'),
    dashTabSchedules: el('dashTabSchedules'),
    dashTabMessages: el('dashTabMessages'),
    dashPanelSchedules: el('dashPanelSchedules'),
    dashPanelMessages: el('dashPanelMessages'),
    homeSaveJobBtn: el('homeSaveJobBtn'),
    homeImportantQuestions: el('homeImportantQuestions'),
    homeGoCandidates: el('homeGoCandidates'),
    homeGoInterviews: el('homeGoInterviews'),
    homeGoAnalytics: el('homeGoAnalytics'),
    statScheduled: el('statScheduled'),
    statCompleted: el('statCompleted'),
    statPending: el('statPending'),
    interviewDetailModal: el('interviewDetailModal'),
    closeInterviewDetailModal: el('closeInterviewDetailModal'),
    closeInterviewDetailModalFooter: el('closeInterviewDetailModalFooter'),
    interviewDetailMeta: el('interviewDetailMeta'),
    tabBtnPerformance: el('tabBtnPerformance'),
    tabBtnTranscript: el('tabBtnTranscript'),
    tabBtnMessages: el('tabBtnMessages'),
    tabPerformance: el('tabPerformance'),
    tabTranscript: el('tabTranscript'),
    tabMessages: el('tabMessages'),
    interviewPerformanceText: el('interviewPerformanceText'),
    interviewTranscriptList: el('interviewTranscriptList'),
    interviewMessagesList: el('interviewMessagesList'),

    atsFinalModal: el('atsFinalModal'),
    atsAddFinalBtn: el('atsAddFinalBtn'),
    closeAtsFinalModal: el('closeAtsFinalModal'),
    cancelAtsFinalBtn: el('cancelAtsFinalBtn'),
    saveAtsFinalBtn: el('saveAtsFinalBtn'),
    atsFinalCandidateId: el('atsFinalCandidateId'),
    atsFinalEmail: el('atsFinalEmail'),
    atsFinalPhone: el('atsFinalPhone'),
    atsFinalNotes: el('atsFinalNotes'),
    atsFinalStatus: el('atsFinalStatus')
  };
  
  // Set global element references
  loadingOverlay = el('loading');
  toastHost = el('toastHost');
  rolePreset = el('rolePreset');
  jdTextarea = el('jd');
  jdCounter = el('jdCounter');
  useExampleBtn = el('useExample');

  elements.themeToggle = el('themeToggle');
  elements.launchApp = el('launchApp');
  elements.seeHowItWorks = el('seeHowItWorks');

  elements.homeGoCandidates2 = el('homeGoCandidates2');
  elements.homeGoCandidates3 = el('homeGoCandidates3');
  elements.homeGoCandidates4 = el('homeGoCandidates4');

  elements.dashSearch = el('dashSearch');
  elements.dashStatus = el('dashStatus');
  elements.scheduleCards = el('scheduleCards');
  elements.kpiScheduled = el('kpiScheduled');
  elements.kpiPending = el('kpiPending');
  elements.kpiCompleted = el('kpiCompleted');

  elements.analyticsSearch = el('analyticsSearch');
  elements.analyticsCards = el('analyticsCards');
  elements.analyticsSpark = el('analyticsSpark');
  elements.analyticsRefresh = el('analyticsRefresh');
  elements.kpiAnalCompleted = el('kpiAnalCompleted');
  elements.kpiAnal7d = el('kpiAnal7d');
  elements.kpiAnal24h = el('kpiAnal24h');
  
  // Add form elements to the elements object
  elements.jdTextarea = jdTextarea;  // Add jdTextarea to elements object
  elements.roleTitle = el('roleTitle');
  elements.reqSkills = el('reqSkills');
  elements.reqCerts = el('reqCerts');
  elements.minExp = el('minExp');
  
  console.log('Elements initialized', elements);
}

function sheetOutreachColumnLetter(index) {
  let value = Number(index) + 1;
  let label = '';
  while (value > 0) {
    const remainder = (value - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    value = Math.floor((value - 1) / 26);
  }
  return label;
}

function resetSheetOutreachPreview(message) {
  sheetOutreachCampaignId = null;
  sheetOutreachRecipientCount = 0;
  const preview = document.getElementById('sheetOutreachPreview');
  const sendBtn = document.getElementById('sheetSendBtn');
  const progress = document.getElementById('sheetOutreachProgress');
  if (preview) preview.hidden = true;
  if (sendBtn) sendBtn.disabled = true;
  if (progress) {
    progress.hidden = true;
    progress.textContent = '';
  }
  if (message) document.getElementById('sheetOutreachStatus').textContent = message;
}

async function inspectSheetOutreachColumns() {
  const fileInput = document.getElementById('sheetOutreachFile');
  const setup = document.getElementById('sheetOutreachSetup');
  const firstNameSelect = document.getElementById('sheetFirstNameColumn');
  const phoneSelect = document.getElementById('sheetPhoneColumn');
  const previewBtn = document.getElementById('sheetPreviewBtn');
  const status = document.getElementById('sheetOutreachStatus');
  const file = fileInput && fileInput.files ? fileInput.files[0] : null;

  sheetOutreachColumnsReady = false;
  resetSheetOutreachPreview('');
  if (setup) setup.hidden = true;
  if (previewBtn) previewBtn.disabled = true;
  if (!file) {
    status.textContent = 'Choose an .xlsx workbook first.';
    return;
  }

  status.textContent = 'Reading column headers…';
  try {
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/api/sms/outreach/columns', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not read columns from this workbook.');

    [firstNameSelect, phoneSelect].forEach((select) => {
      select.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Choose a column';
      select.appendChild(placeholder);
      (data.columns || []).forEach((column) => {
        const option = document.createElement('option');
        option.value = String(column.index);
        option.textContent = column.label + ' (column ' + sheetOutreachColumnLetter(column.index) + ')';
        select.appendChild(option);
      });
      select.disabled = false;
    });

    if (data.suggested_first_name_column !== null && data.suggested_first_name_column !== undefined) {
      firstNameSelect.value = String(data.suggested_first_name_column);
    }
    if (data.suggested_phone_column !== null && data.suggested_phone_column !== undefined) {
      phoneSelect.value = String(data.suggested_phone_column);
    }
    sheetOutreachColumnsReady = true;
    if (setup) setup.hidden = false;
    previewBtn.disabled = !firstNameSelect.value || !phoneSelect.value;
    status.textContent = previewBtn.disabled
      ? 'Columns loaded. Select the first-name and phone columns to continue.'
      : 'Columns loaded. Confirm the mapping and message, then preview recipients.';
  } catch (error) {
    status.textContent = error.message || 'Could not read columns from this workbook.';
  }
}

async function previewSheetOutreach() {
  const fileInput = document.getElementById('sheetOutreachFile');
  const preview = document.getElementById('sheetOutreachPreview');
  const status = document.getElementById('sheetOutreachStatus');
  const previewBtn = document.getElementById('sheetPreviewBtn');
  const sendBtn = document.getElementById('sheetSendBtn');
  const firstNameSelect = document.getElementById('sheetFirstNameColumn');
  const phoneSelect = document.getElementById('sheetPhoneColumn');
  const messageTemplate = document.getElementById('sheetOutreachTemplate').value.trim();
  const file = fileInput && fileInput.files ? fileInput.files[0] : null;
  if (!file || !sheetOutreachColumnsReady) {
    status.textContent = 'Choose a workbook and load its columns first.';
    return;
  }
  if (!firstNameSelect.value || !phoneSelect.value) {
    status.textContent = 'Select both the first-name and phone columns.';
    return;
  }
  if (!messageTemplate) {
    status.textContent = 'Enter the SMS message to send.';
    return;
  }

  preview.hidden = true;
  status.textContent = 'Validating recipients and preparing the preview…';
  previewBtn.disabled = true;
  sendBtn.disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    form.append('first_name_column', firstNameSelect.value);
    form.append('phone_column', phoneSelect.value);
    form.append('message_template', messageTemplate);
    const response = await fetch('/api/sms/outreach/preview', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not read this workbook.');

    sheetOutreachCampaignId = data.campaign_id;
    sheetOutreachRecipientCount = data.recipient_count;
    sheetOutreachLiveEnabled = data.outreach_enabled !== false;
    document.getElementById('sheetOutreachSummary').textContent =
      `${data.recipient_count} unique candidates ready; ${data.skipped_count} rows skipped.`;
    document.getElementById('sheetOutreachMessage').textContent = data.message_preview;
    const samples = document.getElementById('sheetOutreachSamples');
    samples.innerHTML = '';
    (data.sample_recipients || []).forEach((candidate) => {
      const item = document.createElement('li');
      item.innerHTML = `${escapeHtml(candidate.name)} <span class="muted">•••• ${escapeHtml(candidate.phone_last4)}</span>`;
      samples.appendChild(item);
    });
    if (!(data.sample_recipients || []).length) {
      const item = document.createElement('li');
      item.textContent = 'No valid recipients found.';
      samples.appendChild(item);
    }
    const limitInput = document.getElementById('sheetOutreachLimit');
    limitInput.max = String(data.recipient_count);
    limitInput.value = String(data.recipient_count);
    sendBtn.textContent = 'Send ' + data.recipient_count + ' messages';
    sendBtn.disabled = data.recipient_count < 1 || !sheetOutreachLiveEnabled;
    preview.hidden = false;
    status.textContent = sheetOutreachLiveEnabled
      ? 'Preview ready. Nothing has been sent yet.'
      : 'Preview ready, but sheet SMS is disabled. Set SHEET_OUTREACH_ENABLED=1 and restart the app to send.';
  } catch (error) {
    sheetOutreachCampaignId = null;
    status.textContent = error.message || 'Could not read this workbook.';
  } finally {
    previewBtn.disabled = !sheetOutreachColumnsReady || !firstNameSelect.value || !phoneSelect.value;
  }
}

async function sendSheetOutreach() {
  if (!sheetOutreachCampaignId) return;
  const sendBtn = document.getElementById('sheetSendBtn');
  const previewBtn = document.getElementById('sheetPreviewBtn');
  const status = document.getElementById('sheetOutreachStatus');
  const progress = document.getElementById('sheetOutreachProgress');
  const fileInput = document.getElementById('sheetOutreachFile');
  const firstNameSelect = document.getElementById('sheetFirstNameColumn');
  const phoneSelect = document.getElementById('sheetPhoneColumn');
  const messageTemplate = document.getElementById('sheetOutreachTemplate');
  const limitInput = document.getElementById('sheetOutreachLimit');
  const limit = Math.max(1, parseInt(limitInput.value, 10) || 1);
  const selectedLimit = Math.min(limit, sheetOutreachRecipientCount);
  if (!sheetOutreachLiveEnabled) {
    status.textContent = 'Sheet SMS is disabled. Set SHEET_OUTREACH_ENABLED=1 and restart the app to send.';
    return;
  }
  if (!window.confirm('Send ' + selectedLimit + ' SMS messages now through Twilio? This cannot be undone.')) return;
  sendBtn.disabled = true;
  previewBtn.disabled = true;
  fileInput.disabled = true;
  firstNameSelect.disabled = true;
  phoneSelect.disabled = true;
  messageTemplate.disabled = true;
  limitInput.disabled = true;
  sheetOutreachSending = true;
  status.textContent = 'Starting Twilio outreach…';
  progress.hidden = false;
  let sendRequestStarted = false;
  try {
    sendRequestStarted = true;
    const response = await fetch('/api/sms/outreach/' + encodeURIComponent(sheetOutreachCampaignId) + '/send?limit=' + selectedLimit, { method: 'POST' });
    const data = await response.json();
    if (!response.ok) {
      sendRequestStarted = response.status >= 500;
      throw new Error(data.detail || 'Could not start this campaign.');
    }
    let state = data.state;
    while (state === 'sending') {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const statusResponse = await fetch(`/api/sms/outreach/${encodeURIComponent(sheetOutreachCampaignId)}`);
      const statusData = await statusResponse.json();
      if (!statusResponse.ok) throw new Error(statusData.detail || 'Could not read campaign progress.');
      state = statusData.state;
      progress.textContent = `Processed ${statusData.processed_count} of ${statusData.recipient_count} • Twilio accepted ${statusData.sent_count} • failed ${statusData.failed_count}`;
      if (state === 'complete') {
        status.textContent = `Outreach complete. Twilio accepted ${statusData.sent_count}; ${statusData.failed_count} failed; ${statusData.skipped_count} rows skipped.`;
        if (statusData.last_error && statusData.failed_count) {
          progress.textContent += `. Latest error: ${statusData.last_error}`;
        }
      }
    }
  } catch (error) {
    sendBtn.disabled = sendRequestStarted || !sheetOutreachLiveEnabled;
    status.textContent = error.message || 'Outreach could not be started.';
  } finally {
    sheetOutreachSending = false;
    fileInput.disabled = false;
    firstNameSelect.disabled = false;
    phoneSelect.disabled = false;
    messageTemplate.disabled = false;
    limitInput.disabled = false;
    previewBtn.disabled = !sheetOutreachColumnsReady || !firstNameSelect.value || !phoneSelect.value;
  }
}

function showToast(title, message, type, timeoutMs) {
  try {
    if (!toastHost) return;
    var t = String(type || 'ok');
    var ttl = String(title || '');
    var msg = String(message || '');
    var ms = typeof timeoutMs === 'number' ? timeoutMs : 3800;

    var toast = document.createElement('div');
    toast.className = 'toast ' + t;
    toast.innerHTML =
      '<div>' +
      '  <div class="toast-title"></div>' +
      '  <div class="toast-msg"></div>' +
      '</div>' +
      '<div class="toast-actions">' +
      '  <button type="button" class="toast-close" aria-label="Close">✕</button>' +
      '</div>';

    toast.querySelector('.toast-title').textContent = ttl;
    toast.querySelector('.toast-msg').textContent = msg;
    toast.querySelector('.toast-close').addEventListener('click', function () {
      try { toast.remove(); } catch (e) {}
    });

    toastHost.appendChild(toast);

    if (ms > 0) {
      window.setTimeout(function () {
        try { toast.remove(); } catch (e) {}
      }, ms);
    }
  } catch (e) {
    console.warn('Toast failed:', e);
  }
}

function applyTheme(theme) {
  try {
    var t = theme === 'light' ? 'light' : 'dark';
    document.body.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
  } catch (e) {}
}

function initThemeToggle() {
  try {
    var saved = localStorage.getItem('theme') || 'dark';
    applyTheme(saved);
    if (elements.themeToggle) {
      elements.themeToggle.addEventListener('click', function () {
        var current = document.body.getAttribute('data-theme') || 'dark';
        applyTheme(current === 'light' ? 'dark' : 'light');
        showToast('Theme updated', 'Switched to ' + (document.body.getAttribute('data-theme') || 'dark') + ' mode.', 'ok', 2200);
      });
    }
  } catch (e) {
    console.warn('Theme init failed:', e);
  }
}

function openCandidatesPanel() {
  if (elements.resultsPanel) elements.resultsPanel.hidden = false;
  if (elements.resultsPanel) {
    elements.resultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  if (elements.jdTextarea) {
    try { elements.jdTextarea.focus(); } catch (e) {}
  }
}

function openRoleSetup() {
  openCandidatesPanel();
  try {
    var card = document.getElementById('roleSetupCard');
    if (card && typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  } catch (e) {}
  try {
    if (elements.jdTextarea) elements.jdTextarea.focus();
  } catch (e) {}
}

function getJobDescriptionValue() {
  try {
    return (elements.jdTextarea ? elements.jdTextarea.value : '').trim();
  } catch (e) {
    return '';
  }
}

function syncRankEnabledState() {
  try {
    if (!elements.rankBtn) return;
    var hasJd = !!getJobDescriptionValue();
    elements.rankBtn.disabled = !hasJd;
    elements.rankBtn.title = hasJd ? 'Run candidate ranking' : 'Add a job description first';
  } catch (e) {}
}

function initDashboardTabs() {
  if (elements.dashTabSchedules) {
    elements.dashTabSchedules.addEventListener('click', () => setDashboardTab('schedules'));
  }
  if (elements.dashTabMessages) {
    elements.dashTabMessages.addEventListener('click', () => setDashboardTab('messages'));
  }
}

function setNavActive(active) {
  const setBtn = (btn, on) => {
    if (!btn) return;
    btn.classList.toggle('active', !!on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  };
  setBtn(elements.navHome, active === 'home');
  setBtn(elements.navInterviews, active === 'interviews');
  setBtn(elements.navAnalytics, active === 'analytics');
  setBtn(elements.navATS, active === 'ats');
}

function showView(viewName) {
  currentView = viewName;
  setNavActive(viewName);

  if (elements.viewHome) elements.viewHome.hidden = viewName !== 'home';
  if (elements.resultsPanel) elements.resultsPanel.hidden = viewName !== 'candidates';
  if (elements.dashboardPanel) elements.dashboardPanel.hidden = viewName !== 'interviews';
  if (elements.analyticsPanel) elements.analyticsPanel.hidden = viewName !== 'analytics';
  if (elements.atsPanel) elements.atsPanel.hidden = viewName !== 'ats';
  
  if (viewName === 'interviews') {
    loadDashboardData();
  }
  if (viewName === 'analytics') {
    loadAnalyticsData();
  }
  if (viewName === 'ats') {
    loadATSData();
  }
  // Scroll to top for a cleaner app feel
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function loadJobConfig() {
  if (!elements.roleTitle && !elements.jdTextarea && !elements.homeImportantQuestions) return;
  try {
    const res = await fetch('/api/job-config');
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const title = data.title || '';
    const description = data.description || '';
    const qs = Array.isArray(data.important_questions) ? data.important_questions : [];

    // Sync Home form
    if (elements.roleTitle) elements.roleTitle.value = title || elements.roleTitle.value || '';
    if (elements.jdTextarea && description) {
      elements.jdTextarea.value = description;
      updateJdCounter();
    }
    if (elements.homeImportantQuestions) {
      elements.homeImportantQuestions.value = qs.join('\n');
    }
  } catch (e) {
    console.error('Failed to load job config:', e);
  }
}

async function saveJobConfigFromHome() {
  const title = (elements.roleTitle ? elements.roleTitle.value : '').trim();
  const description = (elements.jdTextarea ? elements.jdTextarea.value : '').trim();
  const important_questions = (elements.homeImportantQuestions ? elements.homeImportantQuestions.value : '')
    .split('\n')
    .map(x => x.trim())
    .filter(Boolean);

  try {
    if (elements.statusEl) elements.statusEl.textContent = 'Saving job setup...';
    const res = await fetch('/api/job-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, description, important_questions })
    });
    if (!res.ok) throw new Error(await res.text());
    if (elements.statusEl) elements.statusEl.textContent = 'Job setup saved.';
    showToast('Saved', 'Role setup saved successfully.', 'ok');
  } catch (e) {
    console.error('Failed to save job config from Home:', e);
    if (elements.statusEl) elements.statusEl.textContent = 'Failed to save job setup.';
    showToast('Save failed', 'Could not save role setup. Check server logs.', 'err');
  }
}

function setDashboardTab(tab) {
  const isSchedules = tab === 'schedules';
  if (elements.dashTabSchedules) {
    elements.dashTabSchedules.classList.toggle('active', isSchedules);
    elements.dashTabSchedules.setAttribute('aria-selected', isSchedules ? 'true' : 'false');
  }
  if (elements.dashTabMessages) {
    elements.dashTabMessages.classList.toggle('active', !isSchedules);
    elements.dashTabMessages.setAttribute('aria-selected', !isSchedules ? 'true' : 'false');
  }
  if (elements.dashPanelSchedules) elements.dashPanelSchedules.hidden = !isSchedules;
  if (elements.dashPanelMessages) elements.dashPanelMessages.hidden = isSchedules;
}

function openInterviewDetailModal() {
  if (elements.interviewDetailModal) elements.interviewDetailModal.hidden = false;
}

function closeInterviewDetailModal() {
  if (elements.interviewDetailModal) elements.interviewDetailModal.hidden = true;
}

function setDetailTab(tab) {
  if (!elements.tabBtnPerformance || !elements.tabBtnTranscript || !elements.tabPerformance || !elements.tabTranscript) return;
  const perfActive = tab === 'performance';
  const transcriptActive = tab === 'transcript';
  const messagesActive = tab === 'messages';

  elements.tabBtnPerformance.classList.toggle('active', perfActive);
  elements.tabBtnTranscript.classList.toggle('active', transcriptActive);
  if (elements.tabBtnMessages) elements.tabBtnMessages.classList.toggle('active', messagesActive);

  elements.tabBtnPerformance.setAttribute('aria-selected', perfActive ? 'true' : 'false');
  elements.tabBtnTranscript.setAttribute('aria-selected', transcriptActive ? 'true' : 'false');
  if (elements.tabBtnMessages) elements.tabBtnMessages.setAttribute('aria-selected', messagesActive ? 'true' : 'false');

  elements.tabPerformance.hidden = !perfActive;
  elements.tabTranscript.hidden = !transcriptActive;
  if (elements.tabMessages) elements.tabMessages.hidden = !messagesActive;
}

async function showInterviewDetail(resultId) {
  try {
    setLoading(true);
    const res = await fetch(`/api/interviews/${encodeURIComponent(resultId)}/detail`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    if (elements.interviewDetailMeta) {
      elements.interviewDetailMeta.textContent = `Phone: ${data.phone_number || ''} • Result ID: ${data.id} • Created: ${data.created_at || ''}`;
    }

    if (elements.interviewPerformanceText) {
      const perfRaw = (data.performance && data.performance.raw) ? data.performance.raw : '';
      const fb = data.feedback_text ? `\n\nAI Feedback:\n${data.feedback_text}` : '';
      elements.interviewPerformanceText.textContent = (perfRaw || '(No performance analytics found)') + fb;
    }

    if (elements.interviewTranscriptList) {
      const entries = data.transcript && Array.isArray(data.transcript.entries) ? data.transcript.entries : [];
      if (!entries.length) {
        elements.interviewTranscriptList.innerHTML = '<div class="muted">No transcript found for this interview yet.</div>';
      } else {
        elements.interviewTranscriptList.innerHTML = entries.map((e, idx) => {
          const q = escapeHtml(e.question || '');
          const a = escapeHtml(e.answer || '');
          const ts = escapeHtml(e.timestamp || '');
          return `
            <div class="qa">
              <div class="q">Q${idx + 1}: ${q}</div>
              <div class="a">${a}</div>
              <div class="ts">${ts ? `Timestamp: ${ts}` : ''}</div>
            </div>
          `;
        }).join('');
      }
    }

    if (elements.interviewMessagesList) {
      const msgs = data.messages && Array.isArray(data.messages) ? data.messages : [];
      if (!msgs.length) {
        elements.interviewMessagesList.innerHTML = '<div class="muted">No messages found for this candidate yet.</div>';
      } else {
        elements.interviewMessagesList.innerHTML = msgs.map((m) => {
          const ch = escapeHtml((m.channel || '').toUpperCase());
          const dir = (m.direction || '').toLowerCase();
          const who = dir === 'incoming' ? 'Candidate' : 'Us';
          const msgText = escapeHtml(m.message || '');
          const subj = m.subject ? `<div class="muted">Subject: ${escapeHtml(m.subject || '')}</div>` : '';
          const ts = escapeHtml(String(m.created_at || ''));
          return `
            <div class="qa">
              <div class="q">${ch} • ${escapeHtml(who)} ${ts ? `• ${ts}` : ''}</div>
              ${subj}
              <div class="a">${msgText}</div>
            </div>
          `;
        }).join('');
      }
    }

    setDetailTab('performance');
    openInterviewDetailModal();
  } catch (e) {
    console.error('Failed to load interview detail:', e);
    alert('Failed to load interview detail.');
  } finally {
    setLoading(false);
  }
}

// Preset skills for healthcare roles (match backend list)
const HEALTHCARE_ROLES = {
  'Registered Nurse': [
    'patient care','medication administration','vital signs','wound care','IV therapy',
    'patient assessment','care planning','clinical documentation','BLS','ACLS',
    'patient education','medication management','nursing process','care coordination'
  ],
  'Physician': [
    'diagnosis','treatment planning','patient consultation','medical history',
    'physical examination','medical diagnosis','treatment plans','prescription',
    'medical procedures','patient management','clinical research','medical records'
  ],
  'Medical Assistant': [
    'vital signs','patient intake','medical records','appointment scheduling',
    'specimen collection','EKG','phlebotomy','injections','medical terminology',
    'insurance verification','patient preparation','clinical procedures'
  ],
  'Physical Therapist': [
    'patient assessment','treatment planning','therapeutic exercises','manual therapy',
    'patient education','rehabilitation','mobility training','pain management',
    'exercise prescription','functional training','modalities','patient evaluation'
  ],
  'Radiologic Technologist': [
    'x-ray','radiography','patient positioning','radiation safety','imaging procedures',
    'contrast media','patient care','equipment operation','image quality','CT','MRI',
    'patient safety','medical imaging'
  ]
};

function initTopScrollSync() {
  if (syncInitialized) return;
  const top = document.getElementById('topScroll');
  const topInner = document.getElementById('topScrollInner');
  const wrap = document.getElementById('tableWrapper');
  if (!top || !topInner || !wrap) return;

  const sync = () => {
    top.scrollLeft = wrap.scrollLeft;
  };
  const syncReverse = () => {
    wrap.scrollLeft = top.scrollLeft;
  };
  wrap.addEventListener('scroll', sync);
  top.addEventListener('scroll', syncReverse);

  const setWidths = () => {
    topInner.style.width = wrap.scrollWidth + 'px';
  };
  setWidths();
  window.addEventListener('resize', setWidths);
  syncInitialized = true;
}

async function rankCandidates() {
  // Ensure elements are initialized
  if (!elements.jdTextarea) {
    console.error('Job description element not found');
    return;
  }
  
  const jd = elements.jdTextarea.value.trim();
  if (!jd) { 
    if (elements.statusEl) elements.statusEl.textContent = 'Please paste a job description.'; 
    showToast('Missing job description', 'Paste the job description first, then click Run Ranking.', 'warn');
    return; 
  }
  
  // Reset pagination
  currentPage = 1;
  if (elements.statusEl) elements.statusEl.textContent = 'Ranking candidates...';
  setLoading(true);
  
  // Show loading indicator
  if (elements.resultsPanel) {
    elements.resultsPanel.hidden = false;
    if (elements.resultsTable && elements.resultsTable.parentNode) {
      elements.resultsTable.parentNode.innerHTML = '<div class="loading">Searching for matching candidates...</div>';
    }
  } else {
    console.error('Results container not found');
  }
  
  try {
    const roleTitle = elements.roleTitle ? elements.roleTitle.value.trim() : '';
    const reqSkillsRaw = elements.reqSkills ? elements.reqSkills.value.trim() : '';
    const reqCertsRaw = elements.reqCerts ? elements.reqCerts.value.trim() : '';
    const minExpVal = elements.minExp ? elements.minExp.value.trim() : '';
    
    // Prepare request data - don't include top_k to get all results
    const requestData = {
      job_description: jd,
      // Only include role_title if provided
      ...(roleTitle && { role_title: roleTitle }),
      // Only include required_skills if any are provided
      ...(reqSkillsRaw && { 
        required_skills: reqSkillsRaw.split(',').map(s => s.trim()).filter(Boolean) 
      }),
      // Only include certifications if any are provided
      ...(reqCertsRaw && {
        certifications: reqCertsRaw.split(',').map(s => s.trim()).filter(Boolean)
      }),
      // Only include min_experience_years if provided
      ...(minExpVal && { 
        min_experience_years: parseInt(minExpVal, 10) 
      })
    };
    
    console.log('Sending request (no top_k limit):', JSON.stringify(requestData, null, 2));
    
    console.log('Sending request:', requestData);
    
    const res = await fetch('/rank', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestData)
    });
    
    if (!res.ok) {
      const errorText = await res.text();
      console.error('API Error Status:', res.status, res.statusText);
      console.error('Error Response:', errorText);
      throw new Error(`API Error (${res.status}): ${errorText}`);
    }
    
    const data = await res.json();
    
    // Log detailed response info
    console.group('API Response Details');
    console.log('Response Status:', res.status, res.statusText);
    console.log('Content-Type:', res.headers.get('content-type'));
    console.log('Response Length (chars):', JSON.stringify(data).length);
    console.log('Response Item Count:', Array.isArray(data) ? data.length : 'Not an array');
    
    if (Array.isArray(data)) {
      console.log('First 5 items:', data.slice(0, 5).map(d => ({
        id: d.id,
        name: d.name,
        score: d.score,
        job_title: d.job_title,
        matched_skills: d.matched_skills
      })));
      
      if (data.length > 100) {
        console.warn(`Received ${data.length} results, but only showing first 100 in the UI`);
      }
    } else {
      console.error('Unexpected API response format:', data);
      throw new Error('Received invalid data format from server');
    }
    console.groupEnd();
    
    if (!Array.isArray(data)) {
      throw new Error('Received invalid data format from server');
    }
    
    console.log(`Found ${data.length} candidates`);
    
    allResults = data;
    totalResults = data.length;
    
    console.log('Received data from API:', {
      count: data.length,
      firstCandidate: data[0],
      lastCandidate: data[data.length - 1]
    });
    
    if (totalResults === 0) {
      if (elements.statusEl) elements.statusEl.textContent = 'No matching candidates found. Try adjusting your search criteria.';
      if (elements.resultsPanel) elements.resultsPanel.hidden = true;
      filteredResults = []; // Ensure filteredResults is empty
    } else {
      // Don't show the count here, let applyFilter handle it after filtering
      if (elements.statusEl) elements.statusEl.textContent = 'Processing candidates...';
      if (elements.resultsPanel) elements.resultsPanel.hidden = false;
      
      // Reset filteredResults before applying new filter
      filteredResults = [];
      
      // Apply filter and show first page of results
      applyFilter();
      
      // Initialize scroll sync if elements exist
      initTopScrollSync();
      
      // Log results summary - use filteredResults count now that it's been updated
      console.log(`Found ${filteredResults.length} matching candidates. Showing page ${currentPage} of ${Math.ceil(filteredResults.length / perPage)}`);
    }
  } catch (e) {
    console.error('Error in rankCandidates:', e);
    if (elements.statusEl) {
      elements.statusEl.textContent = `Error: ${e.message || 'Failed to rank candidates'}`;
    }
  } finally {
    setLoading(false);
  }
}

// Availability modal controls
async function openAvailabilityModal() {
  if (!elements.availabilityModal) return;

  try {
    setLoading(true);
    const res = await fetch('/api/availability/recent?limit=50');
    if (!res.ok) {
      throw new Error(await res.text());
    }
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];

    const tbody = elements.availabilityTable;
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!items.length) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="6" class="no-results">No availability records found yet.</td>';
      tbody.appendChild(tr);
    } else {
      items.forEach(item => {
        const tr = document.createElement('tr');
        const availability = item.availability || {};
        const slots = Array.isArray(availability.slots) ? availability.slots : [];
        let slotText = '';
        if (slots.length) {
          const s = slots[0] || {};
          const parts = [];
          if (s.date) parts.push(s.date);
          if (s.start_time && s.end_time) {
            parts.push(`from ${s.start_time} to ${s.end_time}`);
          } else if (s.start_time) {
            parts.push(`after ${s.start_time}`);
          }
          if (s.timezone) parts.push(s.timezone);
          slotText = parts.join(' ');
        }

        tr.innerHTML = `
          <td>${escapeHtml(item.candidate_name || item.candidate_id || 'Unknown')}</td>
          <td>${escapeHtml(item.role || '')}</td>
          <td>${escapeHtml(item.phone || '')}</td>
          <td>${escapeHtml(slotText || '')}</td>
          <td>${escapeHtml(item.raw_message || '')}</td>
          <td>${escapeHtml(item.created_at || '')}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    elements.availabilityModal.hidden = false;
  } catch (e) {
    console.error('Failed to load recent availability:', e);
    alert('Failed to load recent availability records.');
  } finally {
    setLoading(false);
  }
}

function closeAvailabilityModal() {
  if (elements.availabilityModal) {
    elements.availabilityModal.hidden = true;
  }
}

// Dashboard: load SMS logs, schedules, and interview results
async function loadDashboardData() {
  if (!elements.dashboardPanel) return;

  try {
    setLoading(true);

    const [smsRes, schedRes, resultsRes] = await Promise.all([
      fetch('/api/dashboard/sms/logs?limit=100'),
      fetch('/api/dashboard/interviews/schedules?limit=100'),
      fetch('/api/dashboard/interviews/results?limit=100'),
    ]);

    const smsData = smsRes.ok ? await smsRes.json() : { items: [] };
    const schedData = schedRes.ok ? await schedRes.json() : { items: [] };
    const resultsData = resultsRes.ok ? await resultsRes.json() : { items: [] };

    const smsItems = smsData.items || [];
    const schedItems = schedData.items || [];
    const resultItems = resultsData.items || [];

    _dashboardState.sms = smsItems;
    _dashboardState.schedules = schedItems;
    _dashboardState.results = resultItems;

    renderSmsLogTable(smsItems);
    renderScheduleTable(schedItems);

    renderDashboardCards();

    // Update summary cards
    const scheduledCount = schedItems.filter(i => (i.status || '').toLowerCase() === 'scheduled').length;
    const pendingCount = schedItems.filter(i => (i.status || '').toLowerCase() === 'pending').length;
    const completedCount = resultItems.length;

    try {
      if (elements.kpiScheduled) elements.kpiScheduled.textContent = String(scheduledCount);
      if (elements.kpiPending) elements.kpiPending.textContent = String(pendingCount);
      if (elements.kpiCompleted) elements.kpiCompleted.textContent = String(completedCount);
    } catch (e) {}

    if (elements.statScheduled) {
      const valueEl = elements.statScheduled.querySelector('.card-value');
      if (valueEl) valueEl.textContent = String(scheduledCount);
    }
    if (elements.statPending) {
      const valueEl = elements.statPending.querySelector('.card-value');
      if (valueEl) valueEl.textContent = String(pendingCount);
    }
    if (elements.statCompleted) {
      const valueEl = elements.statCompleted.querySelector('.card-value');
      if (valueEl) valueEl.textContent = String(completedCount);
    }

    elements.dashboardPanel.hidden = false;
    setDashboardTab('schedules');
  } catch (e) {
    console.error('Failed to load dashboard data:', e);
    alert('Failed to load dashboard data.');
  } finally {
    setLoading(false);
  }
}

async function loadAnalyticsData() {
  if (!elements.analyticsPanel) return;
  try {
    setLoading(true);
    const resultsRes = await fetch('/api/dashboard/interviews/results?limit=250');
    const resultsData = resultsRes.ok ? await resultsRes.json() : { items: [] };
    const resultItems = resultsData.items || [];

    _analyticsState.results = resultItems;
    _analyticsState.filtered = resultItems;

    renderAnalyticsCards();
    renderAnalyticsSpark();
    syncAnalyticsKpis();

    renderResultsSummaryTable(resultItems);

    elements.analyticsPanel.hidden = false;
  } catch (e) {
    console.error('Failed to load analytics data:', e);
    alert('Failed to load analytics data.');
  } finally {
    setLoading(false);
  }
}

async function loadATSData() {
  if (!elements.atsPanel) return;
  try {
    setLoading(true);
    await loadCeipalDashboard();
    elements.atsPanel.hidden = false;
  } catch (e) {
    console.error('Failed to load ATS data:', e);
    alert('Failed to load ATS data.');
  } finally {
    setLoading(false);
  }
}

function renderDashboardCards() {
  try {
    if (!elements.scheduleCards) return;
    const query = (elements.dashSearch ? elements.dashSearch.value : '').trim().toLowerCase();
    const status = (elements.dashStatus ? elements.dashStatus.value : 'all').toLowerCase();

    const items = Array.isArray(_dashboardState.schedules) ? _dashboardState.schedules : [];
    const filtered = items.filter((i) => {
      const st = (i.status || '').toLowerCase();
      if (status !== 'all' && st !== status) return false;
      if (!query) return true;
      const hay = [
        i.candidate_name,
        i.candidate_phone,
        i.timezone,
        i.scheduled_datetime,
        i.status,
      ].join(' ').toLowerCase();
      return hay.includes(query);
    });

    if (!filtered.length) {
      elements.scheduleCards.innerHTML = '<div class="muted">No matching schedules.</div>';
      return;
    }

    elements.scheduleCards.innerHTML = '';
    filtered.slice(0, 30).forEach((item) => {
      const st = (item.status || '').toLowerCase();
      const pillClass = st === 'scheduled' ? 'ok' : (st === 'pending' ? 'warn' : '');
      const when = item.scheduled_datetime || '';
      const name = item.candidate_name || 'Candidate';
      const phone = item.candidate_phone || '';
      const tz = item.timezone || '';

      const card = document.createElement('div');
      card.className = 'schedule-card';
      card.innerHTML = `
        <div class="schedule-top">
          <div>
            <div class="schedule-when">${escapeHtml(String(when))}</div>
            <div class="schedule-meta">${escapeHtml(name)} • ${escapeHtml(phone)} • ${escapeHtml(tz)}</div>
          </div>
          <div class="pill ${pillClass}">${escapeHtml(item.status || '')}</div>
        </div>
        <div class="schedule-actions">
          <button class="btn btn-secondary btn-sm" type="button" data-copy="${escapeHtml(String(phone))}">Copy phone</button>
        </div>
      `;

      const copyBtn = card.querySelector('button[data-copy]');
      if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(phone);
            showToast('Copied', 'Phone copied to clipboard.', 'ok', 1800);
          } catch (e) {
            showToast('Copy failed', 'Could not copy phone.', 'err', 2200);
          }
        });
      }

      elements.scheduleCards.appendChild(card);
    });
  } catch (e) {
    console.warn('renderDashboardCards failed:', e);
  }
}

function syncAnalyticsKpis() {
  try {
    const items = Array.isArray(_analyticsState.results) ? _analyticsState.results : [];
    const now = Date.now();
    const dayMs = 24 * 60 * 60 * 1000;
    const last24 = items.filter((i) => {
      const t = Date.parse(i.created_at || '') || 0;
      return t && (now - t) <= dayMs;
    }).length;
    const last7 = items.filter((i) => {
      const t = Date.parse(i.created_at || '') || 0;
      return t && (now - t) <= 7 * dayMs;
    }).length;

    if (elements.kpiAnalCompleted) elements.kpiAnalCompleted.textContent = String(items.length);
    if (elements.kpiAnal24h) elements.kpiAnal24h.textContent = String(last24);
    if (elements.kpiAnal7d) elements.kpiAnal7d.textContent = String(last7);
  } catch (e) {}
}

function renderAnalyticsSpark() {
  try {
    if (!elements.analyticsSpark) return;
    const items = Array.isArray(_analyticsState.results) ? _analyticsState.results : [];
    const buckets = new Array(14).fill(0);
    const now = Date.now();
    const dayMs = 24 * 60 * 60 * 1000;

    items.forEach((i) => {
      const t = Date.parse(i.created_at || '') || 0;
      if (!t) return;
      const daysAgo = Math.floor((now - t) / dayMs);
      if (daysAgo >= 0 && daysAgo < 14) {
        buckets[13 - daysAgo] += 1;
      }
    });

    const max = Math.max(1, ...buckets);
    elements.analyticsSpark.innerHTML = '';
    buckets.forEach((v) => {
      const bar = document.createElement('div');
      bar.className = 'sbar';
      bar.style.height = `${Math.max(10, Math.round((v / max) * 70))}%`;
      elements.analyticsSpark.appendChild(bar);
    });
  } catch (e) {
    console.warn('renderAnalyticsSpark failed:', e);
  }
}

function renderAnalyticsCards() {
  try {
    const items = Array.isArray(_analyticsState.results) ? _analyticsState.results : [];
    const q = (elements.analyticsSearch ? elements.analyticsSearch.value : '').trim().toLowerCase();

    const filtered = items.filter((i) => {
      if (!q) return true;
      const hay = [i.phone_number, i.feedback_text, i.created_at].join(' ').toLowerCase();
      return hay.includes(q);
    });
    _analyticsState.filtered = filtered;

    if (!elements.analyticsCards) return;
    if (!filtered.length) {
      elements.analyticsCards.innerHTML = '<div class="muted">No completed interviews match your search.</div>';
      return;
    }

    elements.analyticsCards.innerHTML = '';
    filtered.slice(0, 24).forEach((item) => {
      const created = item.created_at || '';
      const phone = item.phone_number || '';
      const summary = (item.feedback_text || '').trim();
      const short = summary.length > 220 ? summary.slice(0, 220) + '…' : summary;

      const card = document.createElement('div');
      card.className = 'result-card';
      card.innerHTML = `
        <div class="rc-top">
          <div>
            <div class="rc-title">${escapeHtml(phone || 'Completed interview')}</div>
            <div class="rc-sub">${escapeHtml(String(created))}</div>
          </div>
          <button class="btn btn-secondary btn-sm" type="button" data-result-id="${escapeHtml(String(item.id))}">View</button>
        </div>
        <div class="rc-body">${escapeHtml(short || 'No summary available.')}</div>
      `;

      const btn = card.querySelector('button[data-result-id]');
      if (btn) {
        btn.addEventListener('click', () => {
          const id = btn.getAttribute('data-result-id');
          if (id) showInterviewDetail(id);
        });
      }

      elements.analyticsCards.appendChild(card);
    });
  } catch (e) {
    console.warn('renderAnalyticsCards failed:', e);
  }
}

function renderSmsLogTable(items) {
  const tbody = document.querySelector('#smsLogTable tbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  if (!items.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="no-results">No SMS activity yet.</td>';
    tbody.appendChild(tr);
    return;
  }

  items.forEach((item) => {
    const tr = document.createElement('tr');
    const created = item.created_at || '';
    const msg = (item.message || '').slice(0, 120);
    tr.innerHTML = `
      <td>${escapeHtml(String(created))}</td>
      <td>${escapeHtml(item.direction || '')}</td>
      <td>${escapeHtml(item.phone || '')}</td>
      <td title="${escapeHtml(item.message || '')}">${escapeHtml(msg)}</td>
      <td>${escapeHtml(item.status || '')}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderScheduleTable(items) {
  const tbody = document.querySelector('#scheduleTable tbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  if (!items.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="no-results">No interviews scheduled yet.</td>';
    tbody.appendChild(tr);
    return;
  }

  items.forEach((item) => {
    const tr = document.createElement('tr');
    const when = item.scheduled_datetime || '';
    const name = item.candidate_name || '';
    tr.innerHTML = `
      <td>${escapeHtml(String(when))}</td>
      <td>${escapeHtml(name || '')}</td>
      <td>${escapeHtml(item.candidate_phone || '')}</td>
      <td>${escapeHtml(item.timezone || '')}</td>
      <td>${escapeHtml(item.status || '')}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderResultsSummaryTable(items) {
  const tbody = document.querySelector('#resultsSummaryTable tbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  if (!items.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="3" class="no-results">No completed interviews yet.</td>';
    tbody.appendChild(tr);
    return;
  }

  items.forEach((item) => {
    const tr = document.createElement('tr');
    const created = item.created_at || '';
    const feedback = (item.feedback_text || '').slice(0, 160);
    tr.innerHTML = `
      <td>${escapeHtml(String(created))}</td>
      <td>${escapeHtml(item.phone_number || '')}</td>
      <td title="${escapeHtml(item.feedback_text || '')}">${escapeHtml(feedback)}</td>
      <td><button class="btn btn-secondary btn-sm" data-result-id="${escapeHtml(String(item.id))}">View</button></td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('button[data-result-id]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const id = e.currentTarget.getAttribute('data-result-id');
      if (id) showInterviewDetail(id);
    });
  });
}

// (dashboard button wiring happens in initEventListeners after elements init)

async function quickSend() {
  const ids = selectedIds();
  if (!ids.length) { alert('Select at least one candidate.'); return; }
  const defaultSubject = 'Quick check on your availability';
  const defaultBody = `<p>Hi {{name}},</p>
<p>Hope you're doing well. We have an opportunity that aligns with your background. Are you available for a quick chat this week?</p>
<p>Please reply with a few time slots that work for you.</p>
<p>Best regards,<br/>Talent Team</p>`;

  const payload = { candidate_ids: ids, subject: defaultSubject, body_html: defaultBody };

  // Optional SMTP inputs (if provided)
  const smtpHost = (document.getElementById('smtpHost') || { value: '' }).value.trim();
  const smtpPort = (document.getElementById('smtpPort') || { value: '' }).value.trim();
  const smtpUser = (document.getElementById('smtpUser') || { value: '' }).value.trim();
  const smtpPass = (document.getElementById('smtpPass') || { value: '' }).value.trim();
  const smtpFrom = (document.getElementById('smtpFrom') || { value: '' }).value.trim();
  if (smtpHost && smtpUser && smtpPass && smtpFrom) {
    payload.smtp_host = smtpHost;
    if (smtpPort) payload.smtp_port = parseInt(smtpPort, 10);
    payload.smtp_user = smtpUser;
    payload.smtp_pass = smtpPass;
    payload.smtp_from = smtpFrom;
  }

  try {
    const res = await fetch('/email/send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    alert(`Email status: ${data.status}. Sent: ${data.sent || data.count || 0}`);
  } catch (e) {
    console.error(e);
    alert('Failed to send emails.');
  }
}

function renderResults(list) {
  console.log('Rendering results with list:', list);
  // Ensure the results panel exists
  if (!elements.resultsPanel) {
    console.error('Results panel not found');
    return;
  }

  // Ensure the table exists
  if (!elements.resultsTable || !elements.resultsTable.parentNode) {
    console.log('Initializing results table...');
    
    // Create table container
    const tableContainer = document.createElement('div');
    tableContainer.className = 'table-container';
    
    // Create the table
    const table = document.createElement('table');
    table.id = 'resultsTable';
    table.className = 'results-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th><input type="checkbox" id="selectAll"></th>
          <th>Name</th>
          <th>Email</th>
          <th>Job Title</th>
          <th>Skills</th>
          <th>Experience</th>
          <th>Location</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody></tbody>
    `;
    
    // Add table to container
    tableContainer.appendChild(table);
    
    // Clear existing content but preserve the panel header and toolbar
    const panelHeader = elements.resultsPanel.querySelector('.panel-header');
    elements.resultsPanel.innerHTML = '';
    
    // Re-add panel header if it exists, otherwise create a new one
    if (!panelHeader) {
      const newHeader = document.createElement('div');
      newHeader.className = 'panel-header';
      newHeader.innerHTML = `
        <h2>Ranked Candidates</h2>
        <div class="toolbar">
          <button id="quickSendBtn" class="btn btn-primary" title="Send availability email to selected candidates immediately">Quick Send Availability</button>
          <button id="addBestBtn" class="btn btn-secondary" title="Add selected to Best List">Add to Best</button>
          <button id="emailBtn" class="btn btn-primary" title="Send email to selected">Send Email</button>
          <button id="sendMessageBtn" class="btn btn-primary" title="Send SMS to selected">Send SMS</button>
        </div>
      `;
      elements.resultsPanel.appendChild(newHeader);
    } else {
      elements.resultsPanel.appendChild(panelHeader);
    }
    
    // Add the table container
    elements.resultsPanel.appendChild(tableContainer);
    elements.resultsTable = table.querySelector('tbody');
    
    // Re-initialize event listeners for the buttons
    if (!elements.quickSendBtn) {
      elements.quickSendBtn = document.getElementById('quickSendBtn');
      if (elements.quickSendBtn) {
        elements.quickSendBtn.addEventListener('click', quickSend);
      }
    }
    if (!elements.addBestBtn) {
      elements.addBestBtn = document.getElementById('addBestBtn');
      if (elements.addBestBtn) {
        elements.addBestBtn.addEventListener('click', addToBest);
      }
    }
    if (!elements.emailBtn) {
      elements.emailBtn = document.getElementById('emailBtn');
      if (elements.emailBtn) {
        elements.emailBtn.addEventListener('click', openEmailModal);
      }
    }
    if (!elements.sendMessageBtn) {
      elements.sendMessageBtn = document.getElementById('sendMessageBtn');
      if (elements.sendMessageBtn) {
        elements.sendMessageBtn.addEventListener('click', openSmsModal);
      }
    }
  }
  
  const tbody = elements.resultsTable;
  
  // Clear existing rows
  tbody.innerHTML = '';
  
  if (!list || list.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="8" class="no-results">No candidates found matching your criteria.</td>';
    tbody.appendChild(tr);
    return;
  }
  
  list.forEach((candidate) => {
    if (!candidate) return;
    
    const tr = document.createElement('tr');
    tr.className = 'candidate-row';
    tr.setAttribute('data-id', candidate.id || '');
    
    try {
      // Build a human-readable location from city/state/country fields
      let locationParts = [];
      if (candidate.city) locationParts.push(String(candidate.city));
      if (candidate.state) locationParts.push(String(candidate.state));
      if (candidate.country) locationParts.push(String(candidate.country));
      const locationText = locationParts.join(', ');

      tr.innerHTML = `
        <td><input type="checkbox" class="row-select" data-id="${candidate.id || ''}"></td>
        <td class="candidate-name">${escapeHtml(candidate.name || 'N/A')}</td>
        <td class="email">${escapeHtml(candidate.email || 'N/A')}</td>
        <td class="job-title">${escapeHtml(candidate.job_title || 'N/A')}</td>
        <td class="skills">${renderSkillsChips(candidate.skills, candidate.matched_skills || [])}</td>
        <td class="experience">${escapeHtml(candidate.experience || candidate.years_of_experience || 'N/A')}</td>
        <td class="location">${escapeHtml(locationText || 'N/A')}</td>
        <td class="score">${candidate.score ? candidate.score.toFixed(4) : 'N/A'}</td>
      `;
      
      tbody.appendChild(tr);
    } catch (error) {
      console.error('Error rendering candidate:', candidate, error);
    }
  });
  
  // Add event listeners to email buttons
  tbody.querySelectorAll('.email-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const candidateId = e.currentTarget.getAttribute('data-id');
      if (candidateId) {
        openEmailModal(candidateId);
      }
    });
  });
  
  // Update select all checkbox
  updateSelectAllCheckbox();
}

function escapeHtml(str) {
  return str.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function renderSkillsChips(skillsStr, matched = []) {
  if (!skillsStr) return '';
  const skills = skillsStr.split(',').map(s => s.trim()).filter(Boolean);
  const limit = 6;
  const matchedSet = new Set(matched.map(s => s.toLowerCase()));
  const chips = skills.slice(0, limit).map(s => {
    const isMatch = matchedSet.has(s.toLowerCase());
    return `<span class="chip ${isMatch ? 'match' : ''}" title="${escapeHtml(s)}">${escapeHtml(s)}</span>`;
  }).join('');
  const moreCount = Math.max(0, skills.length - limit);
  const moreChip = moreCount > 0 ? `<span class="chip more" data-all="${escapeHtml(skills.join(', '))}">+${moreCount} more</span>` : '';
  return `<div class="skills-chips">${chips}${moreChip}</div>`;
}

// JD character/word counter
function updateJdCounter() {
  if (!jdCounter) return;
  const text = jdTextarea?.value || '';
  const chars = text.length;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  jdCounter.textContent = `${chars} characters, ${words} words`;
}

// Update the select all checkbox state
function updateSelectAllCheckbox() {
  if (!elements.selectAll) return;
  
  const checkboxes = document.querySelectorAll('.row-select:not([disabled])');
  if (checkboxes.length === 0) {
    elements.selectAll.checked = false;
    elements.selectAll.disabled = true;
    return;
  }
  
  elements.selectAll.disabled = false;
  const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
  elements.selectAll.checked = checkedCount > 0 && checkedCount === checkboxes.length;
  elements.selectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
}

// Preset change -> autofill role title and required skills
rolePreset?.addEventListener('change', () => {
  const val = rolePreset.value;
  if (!val || !HEALTHCARE_ROLES[val]) return;
  const roleTitleInput = el('roleTitle');
  const reqSkillsInput = el('reqSkills');
  if (roleTitleInput) roleTitleInput.value = val;
  if (reqSkillsInput) reqSkillsInput.value = HEALTHCARE_ROLES[val].join(', ');
});

// Use Example button -> fill JD and fields
useExampleBtn?.addEventListener('click', () => {
  const roleTitleInput = el('roleTitle');
  const reqSkillsInput = el('reqSkills');
  const minExpInput = el('minExp');
  const exampleRole = rolePreset?.value && HEALTHCARE_ROLES[rolePreset.value] ? rolePreset.value : 'Registered Nurse';
  if (roleTitleInput) roleTitleInput.value = exampleRole;
  if (reqSkillsInput) reqSkillsInput.value = HEALTHCARE_ROLES[exampleRole].join(', ');
  if (minExpInput) minExpInput.value = 3;
  if (jdTextarea) {
    jdTextarea.value = `We are hiring a ${exampleRole} responsible for high quality patient care, documentation, and collaboration with the care team. The role involves shift-based work, adherence to HIPAA, and excellent communication.`;
    updateJdCounter();
  }
});

jdTextarea?.addEventListener('input', updateJdCounter);
updateJdCounter();

function selectedIds() {
  return Array.from(document.querySelectorAll('.row-select:checked')).map(x => x.dataset.id);
}

async function addToBest() {
  const listName = el('bestListName').value.trim() || 'Top-100';
  const ids = selectedIds();
  if (!ids.length) { alert('Select at least one candidate.'); return; }
  try {
    const res = await fetch('/best/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ list_name: listName, candidate_ids: ids })
    });
    if (!res.ok) throw new Error(await res.text());
    alert('Added to best list.');
  } catch (e) {
    console.error(e);
    alert('Failed to add to best list.');
  }
}

async function viewBest() {
  const listName = el('bestListName').value.trim() || 'Top-100';
  try {
    const res = await fetch(`/best/${encodeURIComponent(listName)}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    // Render in table (no scores when viewing from best list)
    const items = (data.candidates || []).map(c => ({...c, score: 0}));
    allResults = items;
    applyFilter();
    goToPage(1);
    resultsPanel.hidden = false;
  } catch (e) {
    console.error(e);
    alert('Failed to load best list.');
  }
}

// Modal controls
const emailModal = document.getElementById('emailModal');
const emailSubject = document.getElementById('emailSubject');
const emailBody = document.getElementById('emailBody');

function openEmailModal() { emailModal.hidden = false; }
function closeEmailModal() { emailModal.hidden = true; }

async function sendEmail() {
  const ids = selectedIds();
  if (!ids.length) { 
    alert('Please select at least one candidate.'); 
    return; 
  }
  
  // Basic payload with required fields
  const payload = {
    candidate_ids: ids,
    subject: emailSubject.value || 'Opportunity with our team',
    body_html: emailBody.value || 'Hello {{name}}',
  };
  
  try {
    const res = await fetch('/email/send', {
      method: 'POST', 
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    
    const data = await res.json();
    
    if (!res.ok) {
      // If there's an error, show the error message from the server
      const errorMsg = data.detail || 'Failed to send emails.';
      throw new Error(errorMsg);
    }
    
    // Show success message with details
    const successMsg = `Successfully sent ${data.sent} email(s).`;
    if (data.errors && data.errors.length > 0) {
      alert(`${successMsg} Failed to send ${data.errors.length} email(s).`);
      console.error('Failed emails:', data.errors);
    } else {
      alert(successMsg);
    }
    
    closeEmailModal();
  } catch (e) {
    console.error('Email sending error:', e);
    alert(`Failed to send emails: ${e.message}`);
  }
}

// SMS Modal Controls
function openSmsModal() {
  const selected = selectedIds();
  if (selected.length === 0) {
    alert('Please select at least one candidate');
    return;
  }
  
  // Update the recipient info display
  const recipientInfo = document.getElementById('smsRecipientInfo');
  if (recipientInfo) {
    if (selected.length === 1) {
      const candidate = allResults.find(c => c.id === selected[0]);
      if (candidate) {
        let phone = candidate.phone || '';
        // Normalize phone number for display
        if (phone && !phone.startsWith('+')) {
          if (/^\d+$/.test(phone)) {
            if (phone.length === 10) {
              phone = `+91${phone}`;
            } else if (phone.length === 12 && phone.startsWith('91')) {
              phone = `+${phone}`;
            }
          }
        }
        elements.smsPhone.value = phone;
        recipientInfo.textContent = `Sending to: ${candidate.name || 'Candidate'} (${phone || 'No phone'})`;
      }
    } else {
      elements.smsPhone.value = '';
      recipientInfo.textContent = `Sending to ${selected.length} selected candidates`;
    }
  }
  
  // Reset message template
  elements.smsMessage.value = 'Hi {{name}}, we\'d like to discuss the {{role}} position. When are you available for a call?\n\nReply STOP to cancel. Reply HELP for help';
  
  // Show the modal
  elements.smsModal.hidden = false;
  
  // Focus the message field
  setTimeout(() => elements.smsMessage.focus(), 100);
}

function closeSmsModal() {
  elements.smsModal.hidden = true;
}

async function sendSms() {
  const message = elements.smsMessage.value.trim();
  const ids = selectedIds();
  const manualNumber = elements.smsPhone.value.trim();

  console.group('SMS Debug');
  console.debug('Selected IDs:', ids);
  console.debug('Manual number:', manualNumber);
  console.debug('allResults length:', allResults ? allResults.length : 'N/A');
  console.debug('originalCandidatesData length:', window.originalCandidatesData && Array.isArray(window.originalCandidatesData)
    ? window.originalCandidatesData.length
    : 'N/A');

  if (!message) {
    alert('Please enter a message');
    console.groupEnd();
    return;
  }

  // Track numbers we're sending to and any errors
  const toNumbers = [];
  const missingNumbers = [];
  const errors = [];
  const results = []; // Initialize results array to store send results
  let totalRecipients = 0;
  let successCount = 0;
  let failCount = 0;

  // Show loading state
  const sendBtn = elements.sendSmsBtn;
  const originalBtnText = sendBtn.textContent;
  sendBtn.disabled = true;
  sendBtn.textContent = 'Sending...';
  
  // Get recipient numbers from selected candidates
  if (ids.length > 0) {
    if (!window.originalCandidatesData || !Array.isArray(window.originalCandidatesData) || !window.originalCandidatesData.length) {
      console.warn('originalCandidatesData is empty; cannot auto-resolve phone numbers');
    }

    // Process each selected candidate
    for (const id of ids) {
      const idStr = String(id);
      let phone = '';
      let candidateName = `Candidate ${idStr}`;

      // Prefer phone/name from the ranked results list (what the user is looking at)
      // since originalCandidatesData may not match IDs.
      let ranked = null;
      if (allResults && allResults.length) {
        ranked = allResults.find(c => c && String(c.id) === idStr) || null;
        if (ranked) {
          if (ranked.name) candidateName = ranked.name;
          if (ranked.phone != null) phone = String(ranked.phone).trim();
        }
      }

      // Try to find the candidate in original data
      let orig = null;
      if (window.originalCandidatesData && Array.isArray(window.originalCandidatesData)) {
        // 1) Match by Sr No.
        orig = window.originalCandidatesData.find(o => {
          try {
            const srNo = o["Sr No."];
            return srNo !== undefined && srNo !== null && String(srNo) === idStr;
          } catch (e) {
            return false;
          }
        });

        // 2) If not found, try match by name from ranked results
        if (!orig && ranked && ranked.name) {
            const rankedName = String(ranked.name).trim().toLowerCase();
            orig = window.originalCandidatesData.find(o => {
              try {
                const fn = o.FirstName ? String(o.FirstName).trim() : '';
                const ln = o.LastName ? String(o.LastName).trim() : '';
                const full = `${fn} ${ln}`.trim().toLowerCase();
                return full && full === rankedName;
              } catch (e) {
                return false;
              }
            });
        }
      }

      // Get phone number
      if (!phone && orig && orig.phone != null) {
        phone = String(orig.phone).trim();
      }

      // If a single candidate is selected, allow the modal's phone input to override.
      // This fixes cases where we display the phone in the modal, but cannot map it
      // back to the original dataset by ID.
      if (ids.length === 1 && manualNumber) {
        phone = manualNumber;
      }

      // Normalize phone number
      if (phone) {
        if (!phone.startsWith('+')) {
          if (/^\d+$/.test(phone)) {
            if (phone.length === 10) {
              phone = `+91${phone}`; // Assume India
            } else if (phone.length === 12 && phone.startsWith('91')) {
              phone = `+${phone}`;
            }
          }
        }
        
        // Personalize message
        let personalizedMessage = message
          .replace(/\{\{\s*name\s*\}\}/gi, candidateName.split(' ')[0])
          .replace(/\{\{\s*role\s*\}\}/gi, elements.roleTitle?.value || 'the position');
          
        toNumbers.push({
          phone,
          name: candidateName,
          message: personalizedMessage,
          id: idStr
        });
      } else {
        missingNumbers.push({
          id: idStr,
          name: candidateName,
          error: 'No phone number found'
        });
      }
    }
  }

  // Add manual number if provided and no candidates selected
  if (ids.length === 0 && manualNumber) {
    let phone = manualNumber;
    // Normalize phone number
    if (phone && !phone.startsWith('+')) {
      if (/^\d+$/.test(phone)) {
        if (phone.length === 10) {
          phone = `+91${phone}`; // Assume India
        } else if (phone.length === 12 && phone.startsWith('91')) {
          phone = `+${phone}`;
        }
      }
    }
    
    toNumbers.push({
      phone: phone,
      name: 'Manual Entry',
      message: message,
      id: 'manual'
    });
  }

  // Check if we have any numbers to send to
  totalRecipients = toNumbers.length + missingNumbers.length;
  
  if (totalRecipients === 0) {
    alert('No valid phone numbers to send to');
    sendBtn.disabled = false;
    sendBtn.textContent = originalBtnText;
    console.groupEnd();
    return;
  }

  // Show confirmation with count
  const confirmed = confirm(`Send this message to ${toNumbers.length} recipient${toNumbers.length !== 1 ? 's' : ''}?${missingNumbers.length > 0 ? `\n\nNote: ${missingNumbers.length} selected ${missingNumbers.length === 1 ? 'candidate has' : 'candidates have'} no phone number and will be skipped.` : ''}`);
  if (!confirmed) {
    console.log('User cancelled sending');
    sendBtn.disabled = false;
    sendBtn.textContent = originalBtnText;
    console.groupEnd();
    return;
  }

  try {
    // Send messages in parallel with rate limiting
    const BATCH_SIZE = 3; // Number of concurrent sends
    const DELAY_BETWEEN_BATCHES = 1000; // 1 second between batches
    
    // Process in batches to avoid rate limiting
    for (let i = 0; i < toNumbers.length; i += BATCH_SIZE) {
      const batch = toNumbers.slice(i, i + BATCH_SIZE);
      const batchPromises = batch.map(recipient => 
        sendSingleSms(recipient.phone, recipient.message, recipient.id)
          .then(result => {
            successCount++;
            return { ...recipient, success: true };
          })
          .catch(error => {
            console.error(`Error sending to ${recipient.phone}:`, error);
            failCount++;
            return { 
              ...recipient, 
              success: false, 
              error: error.message || 'Unknown error' 
            };
          })
      );
      
      // Wait for current batch to complete
      const batchResults = await Promise.all(batchPromises);
      results.push(...batchResults);
      
      // Update status
      updateSendStatus(successCount, failCount, toNumbers.length + missingNumbers.length);
      
      // Add delay between batches if not the last batch
      if (i + BATCH_SIZE < toNumbers.length) {
        await new Promise(resolve => setTimeout(resolve, DELAY_BETWEEN_BATCHES));
      }
    }
    
    // Handle missing numbers as errors
    missingNumbers.forEach(recipient => {
      results.push({
        ...recipient,
        success: false,
        error: 'No phone number available'
      });
      failCount++;
    });
    
    // Show final summary
    showSendSummary(results);
    
    // Close the modal if all went well
    if (failCount === 0) {
      closeSmsModal();
    }
  } catch (error) {
    console.error('Unexpected error in sendSms:', error);
    alert(`An unexpected error occurred: ${error.message}`);
  } finally {
    // Reset button state
    sendBtn.disabled = false;
    sendBtn.textContent = 'Send';
    console.groupEnd();
  }
}

// Helper function to send a single SMS
async function sendSingleSms(phone, message, candidateId) {
  const response = await fetch('/api/sms/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      to_number: phone,
      message: message,
      candidate_id: candidateId
    })
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `HTTP ${response.status}: ${response.statusText}`);
  }
  
  return response.json();
}

// Update status during sending
function updateSendStatus(success, failed, total) {
  const statusEl = document.getElementById('smsSendStatus');
  if (statusEl) {
    statusEl.textContent = `Sending... ${success + failed}/${total} (${success} ✓, ${failed} ✗)`;
  }
}

// Show summary after sending
function showSendSummary(results) {
  const successResults = results.filter(r => r.success);
  const failedResults = results.filter(r => !r.success);
  
  let summary = `Successfully sent to ${successResults.length} ${successResults.length === 1 ? 'recipient' : 'recipients'}`;
  
  if (failedResults.length > 0) {
    summary += `, failed to send to ${failedResults.length} ${failedResults.length === 1 ? 'recipient' : 'recipients'}`;
    
    // Show detailed errors in console
    console.group('Failed sends:');
    failedResults.forEach(r => {
      console.error(`- ${r.name || r.phone || r.id}: ${r.error || 'Unknown error'}`);
    });
    console.groupEnd();
    
    // Show first 3 errors in alert
    const errorList = failedResults
      .slice(0, 3)
      .map(r => `• ${r.name || r.phone || r.id}: ${r.error || 'Unknown error'}`)
      .join('\n');
      
    const moreCount = failedResults.length - 3;
    const moreText = moreCount > 0 ? `\n...and ${moreCount} more. Check console for details.` : '';
    
    alert(`${summary}\n\n${errorList}${moreText}`);
  } else {
    alert(summary);
  }
}

// Initialize event listeners
function initEventListeners() {
  // JD textarea
  if (elements.jdTextarea) {
    elements.jdTextarea.addEventListener('input', () => {
      syncRankEnabledState();
      updateJDCounter();
    });
  }
  
  // Rank button
  if (elements.rankBtn) {
    elements.rankBtn.addEventListener('click', (e) => {
      try {
        if (e && typeof e.preventDefault === 'function') e.preventDefault();
        if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
      } catch (err) {}
      rankCandidates();
    });
  }

  // Navigation
  if (elements.navHome) {
    elements.navHome.addEventListener('click', () => showView('home'));
  }
  if (elements.navInterviews) {
    elements.navInterviews.addEventListener('click', () => showView('interviews'));
  }
  if (elements.navAnalytics) {
    elements.navAnalytics.addEventListener('click', () => showView('analytics'));
  }
  if (elements.navATS) {
    elements.navATS.addEventListener('click', () => showView('ats'));
  }

  // Existing "View Interview Dashboard" button in the candidates toolbar
  if (elements.viewDashboardBtn) {
    elements.viewDashboardBtn.addEventListener('click', () => showView('interviews'));
  }

  if (elements.goAnalyticsBtn) {
    elements.goAnalyticsBtn.addEventListener('click', () => showView('analytics'));
  }

  // Home actions
  if (elements.homeSaveJobBtn) {
    elements.homeSaveJobBtn.addEventListener('click', saveJobConfigFromHome);
  }
  if (elements.homeGoCandidates) {
    elements.homeGoCandidates.addEventListener('click', () => {
      openRoleSetup();
    });
  }
  if (elements.homeGoCandidates2) {
    elements.homeGoCandidates2.addEventListener('click', () => openRoleSetup());
  }
  if (elements.homeGoCandidates3) {
    elements.homeGoCandidates3.addEventListener('click', () => openRoleSetup());
  }
  if (elements.homeGoCandidates4) {
    elements.homeGoCandidates4.addEventListener('click', () => openRoleSetup());
  }
  if (elements.launchApp) {
    elements.launchApp.addEventListener('click', () => {
      openRoleSetup();
      showToast('Launch App', 'Paste your job description to start ranking candidates.', 'ok', 2600);
    });
  }
  if (elements.seeHowItWorks) {
    elements.seeHowItWorks.addEventListener('click', () => {
      try {
        const how = document.getElementById('homeHow');
        if (how) how.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (e) {}
    });
  }

  // FAQ accordion
  try {
    document.querySelectorAll('.faq-item').forEach((btn) => {
      btn.addEventListener('click', () => {
        const expanded = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        const icon = btn.querySelector('.faq-icon');
        if (icon) icon.textContent = expanded ? '+' : '–';
        const ans = btn.nextElementSibling;
        if (ans && ans.classList && ans.classList.contains('faq-a')) {
          ans.hidden = expanded;
        }
      });
    });
  } catch (e) {}

  // Dashboard filters
  if (elements.dashSearch) {
    elements.dashSearch.addEventListener('input', debounce(() => renderDashboardCards(), 200));
  }
  if (elements.dashStatus) {
    elements.dashStatus.addEventListener('change', () => renderDashboardCards());
  }

  // Analytics filters
  if (elements.analyticsSearch) {
    elements.analyticsSearch.addEventListener('input', debounce(() => renderAnalyticsCards(), 200));
  }
  if (elements.analyticsRefresh) {
    elements.analyticsRefresh.addEventListener('click', () => loadAnalyticsData());
  }
  if (elements.ceipalRefresh) {
    elements.ceipalRefresh.addEventListener('click', () => loadCeipalDashboard());
  }

  // ATS final-selected modal
  if (elements.atsAddFinalBtn) {
    elements.atsAddFinalBtn.addEventListener('click', () => openAtsFinalModal({}));
  }
  if (elements.closeAtsFinalModal) {
    elements.closeAtsFinalModal.addEventListener('click', closeAtsFinalModal);
  }
  if (elements.cancelAtsFinalBtn) {
    elements.cancelAtsFinalBtn.addEventListener('click', closeAtsFinalModal);
  }
  if (elements.saveAtsFinalBtn) {
    elements.saveAtsFinalBtn.addEventListener('click', saveAtsFinalSelection);
  }
  if (elements.atsFinalModal) {
    window.addEventListener('click', (e) => {
      if (e.target === elements.atsFinalModal) {
        closeAtsFinalModal();
      }
    });
  }
  if (elements.homeGoInterviews) {
    elements.homeGoInterviews.addEventListener('click', () => showView('interviews'));
  }
  if (elements.homeGoAnalytics) {
    elements.homeGoAnalytics.addEventListener('click', () => showView('analytics'));
  }

  // Interview dashboard tabs
  initDashboardTabs();
  
  // Use example button
  if (useExampleBtn) {
    useExampleBtn.addEventListener('click', () => {
      elements.jdTextarea.value = 'We are looking for an experienced AI Engineer with strong skills in Python, machine learning, and deep learning. The ideal candidate should have experience with LLMs, PyTorch, and natural language processing. Responsibilities include developing and deploying machine learning models, fine-tuning LLMs, and working with large datasets.';
      elements.roleTitle.value = 'AI Engineer';
      elements.reqSkills.value = 'Python, Machine Learning, Deep Learning, LLMs, PyTorch, NLP';
      elements.minExp.value = '3';
      updateJdCounter();
    });
  }
  
  // Add to best button
  if (elements.addBestBtn) {
    elements.addBestBtn.addEventListener('click', addToBest);
  }
  
  // View best button
  if (elements.viewBestBtn) {
    elements.viewBestBtn.addEventListener('click', viewBest);
  }
  
  // Quick send button
  if (elements.quickSendBtn) {
    elements.quickSendBtn.addEventListener('click', quickSend);
  }

  const sheetPreviewBtn = document.getElementById('sheetPreviewBtn');
  const sheetSendBtn = document.getElementById('sheetSendBtn');
  const sheetLimitInput = document.getElementById('sheetOutreachLimit');
  if (sheetPreviewBtn) sheetPreviewBtn.addEventListener('click', previewSheetOutreach);
  if (sheetSendBtn) sheetSendBtn.addEventListener('click', sendSheetOutreach);
  if (sheetLimitInput) {
    sheetLimitInput.addEventListener('input', () => {
      if (!sheetOutreachCampaignId || !sheetSendBtn || sheetOutreachSending) return;
      const max = parseInt(sheetLimitInput.max, 10) || 1;
      const count = Math.min(Math.max(1, parseInt(sheetLimitInput.value, 10) || 1), max);
      sheetSendBtn.textContent = 'Send ' + count + ' messages';
    });
  }
  const sheetFileInput = document.getElementById('sheetOutreachFile');
  if (sheetFileInput) {
    sheetFileInput.addEventListener('change', inspectSheetOutreachColumns);
  }
  const sheetFirstNameColumn = document.getElementById('sheetFirstNameColumn');
  const sheetPhoneColumn = document.getElementById('sheetPhoneColumn');
  const sheetMessageTemplate = document.getElementById('sheetOutreachTemplate');
  const invalidatePreviewOnChange = () => {
    if (sheetOutreachSending) return;
    resetSheetOutreachPreview('Settings changed. Preview the recipients again before sending.');
    const previewBtn = document.getElementById('sheetPreviewBtn');
    if (previewBtn) {
      previewBtn.disabled = !sheetOutreachColumnsReady || !sheetFirstNameColumn.value || !sheetPhoneColumn.value;
    }
  };
  if (sheetFirstNameColumn) sheetFirstNameColumn.addEventListener('change', invalidatePreviewOnChange);
  if (sheetPhoneColumn) sheetPhoneColumn.addEventListener('change', invalidatePreviewOnChange);
  if (sheetMessageTemplate) sheetMessageTemplate.addEventListener('input', invalidatePreviewOnChange);
  const sheetInsertNameBtn = document.getElementById('sheetInsertNameToken');
  if (sheetInsertNameBtn && sheetMessageTemplate) {
    sheetInsertNameBtn.addEventListener('click', () => {
      const token = '{{first_name}}';
      const start = sheetMessageTemplate.selectionStart;
      const end = sheetMessageTemplate.selectionEnd;
      sheetMessageTemplate.setRangeText(token, start, end, 'end');
      sheetMessageTemplate.focus();
      invalidatePreviewOnChange();
    });
  }
  
  // Send message button
  if (elements.sendMessageBtn) {
    elements.sendMessageBtn.addEventListener('click', openSmsModal);
  }

  // View availability button
  if (elements.viewAvailabilityBtn) {
    elements.viewAvailabilityBtn.addEventListener('click', openAvailabilityModal);
  }
  
  // Email button
  if (elements.emailBtn) {
    elements.emailBtn.addEventListener('click', openEmailModal);
  }
  
  // SMS Modal event listeners
  if (elements.closeSmsModal) {
    elements.closeSmsModal.addEventListener('click', closeSmsModal);
  }
  
  if (elements.cancelSmsBtn) {
    elements.cancelSmsBtn.addEventListener('click', closeSmsModal);
  }

  // Availability modal events
  if (elements.closeAvailabilityModal) {
    elements.closeAvailabilityModal.addEventListener('click', closeAvailabilityModal);
  }
  
  if (elements.sendSmsBtn) {
    elements.sendSmsBtn.addEventListener('click', sendSms);
  }
  
  // Close modal when clicking outside
  if (elements.smsModal) {
    window.addEventListener('click', (e) => {
      if (e.target === elements.smsModal) {
        closeSmsModal();
      }
    });
  }

  // Interview detail modal
  if (elements.closeInterviewDetailModal) {
    elements.closeInterviewDetailModal.addEventListener('click', closeInterviewDetailModal);
  }
  if (elements.closeInterviewDetailModalFooter) {
    elements.closeInterviewDetailModalFooter.addEventListener('click', closeInterviewDetailModal);
  }
  if (elements.interviewDetailModal) {
    window.addEventListener('click', (e) => {
      if (e.target === elements.interviewDetailModal) {
        closeInterviewDetailModal();
      }
    });
  }
  if (elements.tabBtnPerformance) {
    elements.tabBtnPerformance.addEventListener('click', () => setDetailTab('performance'));
  }
  if (elements.tabBtnTranscript) {
    elements.tabBtnTranscript.addEventListener('click', () => setDetailTab('transcript'));
  }
  if (elements.tabBtnMessages) {
    elements.tabBtnMessages.addEventListener('click', () => setDetailTab('messages'));
  }
  
  // Pagination controls
  if (elements.pageSize) {
    elements.pageSize.addEventListener('change', (e) => {
      perPage = parseInt(e.target.value, 10);
      localStorage.setItem('perPage', perPage);
      currentPage = 1;
      renderResults(filteredResults);
    });
  }
  
  if (elements.firstPageBtn) {
    elements.firstPageBtn.addEventListener('click', () => goToPage(1));
  }
  
  if (elements.prevPageBtn) {
    elements.prevPageBtn.addEventListener('click', () => goToPage(currentPage - 1));
  }
  
  if (elements.nextPageBtn) {
    elements.nextPageBtn.addEventListener('click', () => goToPage(currentPage + 1));
  }
  
  if (elements.lastPageBtn) {
    elements.lastPageBtn.addEventListener('click', () => goToPage(totalPages));
  }
  
  if (elements.currentPage) {
    elements.currentPage.addEventListener('change', (e) => {
      const page = parseInt(e.target.value, 10);
      if (page >= 1 && page <= totalPages) {
        goToPage(page);
      }
    });
  }
  
  // Filter input
  if (elements.filterInput) {
    elements.filterInput.addEventListener('input', debounce(applyFilter, 300));
  }
  
  // Select all checkbox
  if (elements.selectAll) {
    elements.selectAll.addEventListener('change', (e) => {
      document.querySelectorAll('.row-select').forEach(checkbox => {
        checkbox.checked = e.target.checked;
      });
    });
  }
}

// Initialize the application when the DOM is fully loaded
document.addEventListener('DOMContentLoaded', () => {
  console.log('DOM fully loaded, initializing...');
  
  // Initialize elements
  initializeElements();

  // Theme toggle
  initThemeToggle();

  // Initial gating state
  syncRankEnabledState();
  
  // Set up event listeners
  initEventListeners();
  
  // Load pagination state from URL
  loadPaginationState();
  
  // Initialize the top scroll sync
  initTopScrollSync();
  
  // Initialize JD counter
  updateJdCounter();

  // Start directly in the spreadsheet SMS workflow.
  showView('candidates');

  // Initialize reveal animations on initial load
  setTimeout(initRevealAnimations, 300);

  // Initialize enhanced animations
  initEnhancedAnimations();

  // Subtle scroll-reveal animations on Home
  try {
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduceMotion && 'IntersectionObserver' in window) {
      const nodes = document.querySelectorAll('.marketing-card, .feature, .step-card');
      nodes.forEach((n) => n.classList.add('reveal'));

      const io = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add('in');
            io.unobserve(e.target);
          }
        });
      }, { threshold: 0.12, rootMargin: '0px 0px -10% 0px' });

      nodes.forEach((n) => io.observe(n));
    }
  } catch (e) {
    // Ignore animation failures
  }
  
  // Initialize pagination
  loadPaginationState();
  updatePagination();

  console.log('Application initialized');
});

// Filtering and pagination
function debounce(fn, ms) { let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); }; }

function applyFilter() {
  console.log('Applying filter...');

  try {
    // Sort candidates by score (highest first) and filter by score >= 0.3
    filteredResults = allResults
      .filter(candidate => {
        const score = candidate.score !== undefined ?
                     parseFloat(candidate.score) : 0;
        return score >= 0.3;
      })
      .sort((a, b) => {
        const scoreA = a.score !== undefined ? parseFloat(a.score) : 0;
        const scoreB = b.score !== undefined ? parseFloat(b.score) : 0;
        return scoreB - scoreA; // Sort descending
      });

    console.log(`Found ${filteredResults.length} candidates with score >= 0.3`);

    // Update the UI
    if (elements.resultsPanel) {
      elements.resultsPanel.hidden = false;

      if (filteredResults.length === 0) {
        if (elements.statusEl) {
          elements.statusEl.textContent = 'No candidates found with sufficient match score.';
        }
        renderResults([]);
      } else {
        if (elements.statusEl) {
          elements.statusEl.textContent = `Found ${filteredResults.length} well-matched candidates.`;
        }
        renderResults(filteredResults);
      }
    }

    // Update pagination
    updatePagination();

    if (filteredResults.length > 0) {
      goToPage(1);
    }
  } catch (e) {
    console.error('Error in applyFilter:', e);
    if (elements.statusEl) {
      elements.statusEl.textContent = `Error: ${e.message}`;
    }
  }
}

function updatePagination() {
  const total = filteredResults.length;
  totalPages = Math.max(1, Math.ceil(total / perPage));
  
  // Ensure current page is within bounds
  currentPage = Math.max(1, Math.min(currentPage, totalPages));
  
  // Calculate result range
  const start = total > 0 ? (currentPage - 1) * perPage + 1 : 0;
  const end = Math.min(currentPage * perPage, total);
  
  // Update result count and range
  if (elements.resultCount) {
    elements.resultCount.textContent = `${total} candidate${total !== 1 ? 's' : ''}`;
  }
  
  if (elements.resultRange) {
    elements.resultRange.textContent = total > 0 ? `${start}-${end}` : '0-0';
  }
  
  if (elements.totalResultsEl) {
    elements.totalResultsEl.textContent = total;
  }
  
  // Update page number inputs
  if (elements.currentPage) {
    elements.currentPage.value = currentPage;
    elements.currentPage.max = totalPages;
  }
  
  if (elements.totalPages) {
    elements.totalPages.textContent = totalPages;
  }
  
  // Update button states
  if (elements.firstPageBtn) elements.firstPageBtn.disabled = (currentPage <= 1);
  if (elements.prevPageBtn) elements.prevPageBtn.disabled = (currentPage <= 1);
  if (elements.nextPageBtn) elements.nextPageBtn.disabled = (currentPage >= totalPages);
  if (elements.lastPageBtn) elements.lastPageBtn.disabled = (currentPage >= totalPages);
  
  // Update URL with pagination state
  updateUrlState();
  
  console.log(`Pagination: Page ${currentPage} of ${totalPages}, showing ${end - start + 1} of ${total} results`);
}

function goToPage(page) {
  if (isLoading) {
    console.log('Already loading, skipping page change');
    return; // Prevent multiple rapid clicks
  }
  
  const total = filteredResults.length;
  if (total === 0) {
    console.log('No results to paginate');
    return; // No results to paginate
  }
  
  // Ensure page is within valid range
  const newPage = Math.max(1, Math.min(page, totalPages));
  if (newPage === currentPage) {
    console.log('No page change needed');
    return; // No change
  }
  
  console.log(`Going to page ${newPage} of ${totalPages}`);
  // Update current page
  currentPage = newPage;
  
  // Show loading indicator
  const resultsContainer = elements.results;
  if (resultsContainer) {
    const loadingHtml = `
      <div class="loading-container">
        <div class="spinner"></div>
        <div>Loading page ${currentPage} of ${totalPages}...</div>
      </div>
    `;
    resultsContainer.innerHTML = loadingHtml;
  }
  
  // Use requestAnimationFrame for smoother UI updates
  requestAnimationFrame(() => {
    // Calculate slice of results to show
    const start = (currentPage - 1) * perPage;
    const end = Math.min(start + perPage, total);
    const pageResults = filteredResults.slice(start, end);
    
    // Render the current page of results
    renderResults(pageResults);
    
    // Update pagination UI
    updatePagination();
    
    // Scroll to top of results if not the first page
    if (currentPage > 1 && resultsContainer) {
      resultsContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    
    // Log page change
    console.log(`Navigated to page ${currentPage} of ${totalPages}, showing ${pageResults.length} results`);
  });
}

// Update URL with current pagination state
function updateUrlState() {
  const params = new URLSearchParams(window.location.search);
  params.set('page', currentPage);
  params.set('perPage', perPage);
  
  const newUrl = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState({}, '', newUrl);
}

// Load pagination state from URL
function loadPaginationState() {
  const params = new URLSearchParams(window.location.search);
  const page = parseInt(params.get('page'), 10);
  const size = parseInt(params.get('perPage'), 10);
  
  if (page && !isNaN(page) && page > 0) {
    currentPage = page;
  }
  
  if (size && !isNaN(size) && [10, 25, 50, 100, 250].includes(size)) {
    perPage = size;
    if (elements.pageSize) {
      elements.pageSize.value = perPage;
    }
  }
}

// Event listeners are now handled in initEventListeners()

function setLoading(v) { if (loadingOverlay) loadingOverlay.hidden = !v; }

// ============================================
// ENHANCED ANIMATION UTILITIES
// ============================================

// Animate number counting up
function animateNumber(element, target, duration = 1000, suffix = '') {
  if (!element) return;
  const start = 0;
  const startTime = performance.now();
  
  function update(currentTime) {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const easeOut = 1 - Math.pow(1 - progress, 3);
    const current = Math.floor(start + (target - start) * easeOut);
    element.textContent = current + suffix;
    
    if (progress < 1) {
      requestAnimationFrame(update);
    } else {
      element.textContent = target + suffix;
    }
  }
  requestAnimationFrame(update);
}

// Smooth view transition with animation
function transitionToView(viewName) {
  const views = ['viewHome', 'resultsPanel', 'dashboardPanel', 'analyticsPanel', 'atsPanel'];
  const targetView = {
    'home': 'viewHome',
    'candidates': 'resultsPanel',
    'interviews': 'dashboardPanel',
    'analytics': 'analyticsPanel',
    'ats': 'atsPanel'
  }[viewName];
  
  if (!targetView) return;
  
  // Hide all views with fade out
  views.forEach(id => {
    const el = document.getElementById(id);
    if (el && !el.hidden) {
      el.style.opacity = '0';
      el.style.transform = 'translateY(10px)';
      setTimeout(() => {
        el.hidden = true;
        el.style.opacity = '';
        el.style.transform = '';
      }, 300);
    }
  });
  
  // Show target view with fade in
  setTimeout(() => {
    const target = document.getElementById(targetView);
    if (target) {
      target.hidden = false;
      target.style.opacity = '0';
      target.style.transform = 'translateY(20px)';
      target.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
      
      requestAnimationFrame(() => {
        target.style.opacity = '1';
        target.style.transform = 'translateY(0)';
      });
      
      setTimeout(() => {
        target.style.opacity = '';
        target.style.transform = '';
        target.style.transition = '';
      }, 400);
    }
  }, 300);
}

// Enhanced toast with animation
function showToast(title, message, type = 'info', duration = 5000) {
  if (!toastHost) return;
  
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <div>
      <div class="toast-title">${escapeHtml(title)}</div>
      <div class="toast-msg">${escapeHtml(message)}</div>
    </div>
    <div class="toast-actions">
      <button class="toast-close" aria-label="Close">×</button>
    </div>
  `;
  
  toast.style.opacity = '0';
  toast.style.transform = 'translateX(100px)';
  toastHost.appendChild(toast);
  
  // Animate in
  requestAnimationFrame(() => {
    toast.style.transition = 'all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1)';
    toast.style.opacity = '1';
    toast.style.transform = 'translateX(0)';
  });
  
  // Close button
  const closeBtn = toast.querySelector('.toast-close');
  closeBtn.addEventListener('click', () => {
    removeToast(toast);
  });
  
  // Auto remove
  if (duration > 0) {
    setTimeout(() => removeToast(toast), duration);
  }
  
  return toast;
}

function removeToast(toast) {
  toast.style.transform = 'translateX(100%)';
  toast.style.opacity = '0';
  setTimeout(() => toast.remove(), 300);
}

// Animate table rows on load
function animateTableRows(tableBody) {
  if (!tableBody) return;
  const rows = tableBody.querySelectorAll('tr');
  rows.forEach((row, i) => {
    row.style.opacity = '0';
    row.style.transform = 'translateX(-20px)';
    setTimeout(() => {
      row.style.transition = 'all 0.3s ease';
      row.style.opacity = '1';
      row.style.transform = 'translateX(0)';
    }, i * 50);
  });
}

// Pulse animation for KPI updates
function pulseKPI(element) {
  if (!element) return;
  element.style.transform = 'scale(1.1)';
  element.style.color = 'var(--accent)';
  setTimeout(() => {
    element.style.transform = 'scale(1)';
    element.style.color = '';
  }, 300);
}

// Initialize enhanced animations
function initEnhancedAnimations() {
  // Add hover tilt effect to cards
  document.querySelectorAll('.step-card:not(#roleSetupCard), .feature, .price-card').forEach(card => {
    card.addEventListener('mousemove', (e) => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const centerX = rect.width / 2;
      const centerY = rect.height / 2;
      const rotateX = (y - centerY) / 20;
      const rotateY = (centerX - x) / 20;
      card.style.transform = `perspective(1000px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateY(-2px) scale(1.02)`;
    });
    
    card.addEventListener('mouseleave', () => {
      card.style.transform = '';
    });
  });
  
  // Animate KPI values on load
  document.querySelectorAll('.kpi-value').forEach(kpi => {
    const value = parseInt(kpi.textContent, 10);
    if (!isNaN(value) && value > 0) {
      kpi.textContent = '0';
      setTimeout(() => animateNumber(kpi, value, 1000), 300);
    }
  });
  
  // Add click ripple effect to buttons
  document.querySelectorAll('.btn').forEach(btn => {
    btn.addEventListener('click', function(e) {
      const ripple = document.createElement('span');
      const rect = this.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const x = e.clientX - rect.left - size / 2;
      const y = e.clientY - rect.top - size / 2;
      
      ripple.style.cssText = `
        position: absolute;
        width: ${size}px;
        height: ${size}px;
        left: ${x}px;
        top: ${y}px;
        background: rgba(255,255,255,0.3);
        border-radius: 50%;
        transform: scale(0);
        animation: ripple 0.6s ease-out;
        pointer-events: none;
      `;
      
      this.style.position = 'relative';
      this.style.overflow = 'hidden';
      this.appendChild(ripple);
      
      setTimeout(() => ripple.remove(), 600);
    });
  });
  
  // Add ripple keyframes if not exists
  if (!document.querySelector('#ripple-styles')) {
    const style = document.createElement('style');
    style.id = 'ripple-styles';
    style.textContent = `
      @keyframes ripple {
        to {
          transform: scale(4);
          opacity: 0;
        }
      }
    `;
    document.head.appendChild(style);
  }
}

// Update showView to use smooth transitions
const originalShowView = showView;
showView = function(viewName) {
  transitionToView(viewName);
  
  // Update nav button states
  const navMap = {
    'home': 'navHome',
    'candidates': 'navHome',
    'interviews': 'navInterviews',
    'analytics': 'navAnalytics',
    'ats': 'navATS'
  };
  
  document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
  const activeNav = document.getElementById(navMap[viewName]);
  if (activeNav) activeNav.classList.add('active');
  
  // Load view-specific data
  if (viewName === 'interviews' || viewName === 'dashboard') {
    loadDashboardData();
  } else if (viewName === 'analytics') {
    loadAnalyticsData();
  } else if (viewName === 'ats') {
    loadCeipalDashboard();
  }
  
  currentView = viewName;
};
