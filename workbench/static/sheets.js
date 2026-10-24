'use strict';
(() => {
  const statusLabels = { queued: '대기', running: '실행 중', completed: '완료', finished: '작업 종료', failed: '실패', cancelled: '취소됨', interrupted: '중단됨', submitting: '제출 중', submitted: '제출됨', pending: '남음', blocked: '엔진 없음', prepared: '입력 준비됨', not_created: '만들지 않음' };
  const label = status => escape(statusLabels[status] || status);
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const table = (head, rows) => `<div class="table-scroll"><table class="research-table"><thead><tr>${head.map(h => `<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  const features = items => `<div class="research-features">${items.map(([name, value, note]) => `<div class="research-feature"><span>${escape(name)}</span><strong>${value}</strong>${note ? `<small>${escape(note)}</small>` : ''}</div>`).join('')}</div>`;
  const warnings = list => list?.length ? `<div class="structure-warnings"><strong>주의</strong><ul>${list.map(w => `<li>${escape(w)}</li>`).join('')}</ul></div>` : '';
  let templates = null, templateRequest = null, current = null, timer = null, actionPending = false;

  async function formAction(action) {
    if (actionPending) return;
    actionPending = true;
    const form = $('#sheet-form');
    $$('button', form).forEach(button => { button.disabled = true; });
    $('#sheet-text').readOnly = true;
    form.setAttribute('aria-busy', 'true');
    try { return await action(); }
    finally {
      actionPending = false;
      $$('button', form).forEach(button => { button.disabled = false; });
      $('#sheet-text').readOnly = false;
      form.removeAttribute('aria-busy');
    }
  }

  function selectLine(number) {
    const area = $('#sheet-text'), lines = area.value.replace(/\r\n?/g, '\n').split('\n');
    const start = lines.slice(0, number - 1).reduce((sum, line) => sum + line.length + 1, 0);
    area.focus();
    area.setSelectionRange(start, start + (lines[number - 1] || '').length);
  }
  function errorsHTML(errors) {
    return `<div class="error-message">실행표에 고칠 곳이 ${errors.length}개 있습니다. 줄 번호를 누르면 그 줄로 갑니다.</div>` +
      table(['줄', '내용'], errors.map(e => [e.line ? `<button class="text-button" type="button" data-sheet-line="${e.line}">${e.line}</button>` : '—', escape(e.message)]));
  }
  function settingsText(settings) {
    const temps = settings.temperatures ? settings.temperatures.map(t => formatNumber(t)).join(', ') + ' K' : '250~500 K, 25 K 간격(기본)';
    return `온도 ${temps} · 표본 ${formatNumber(settings.samples)} · 클러스터 ${formatNumber(settings.clusters)} · 시드 ${settings.seed} · 말단 ${settings.cap === 'methyl' ? '메틸' : '수소'} · 접촉배율 ${settings.contact_scale}`;
  }
  function previewHTML(plan) {
    if (!plan.valid) return errorsHTML(plan.errors);
    let html = `<div class="success-message">검사 통과 · ${escape(plan.name)} — 아직 아무것도 실행하지 않았습니다.</div>`;
    if (plan.note) html += `<p class="research-footnote">${escape(plan.note)}</p>`;
    if (plan.kind === 'blends') {
      html += features([['종류', 'Blends χ'], ['조합 수', fmt(plan.estimate.pairs), '기준 × 대상'], ['접촉 표본 합계', fmt(plan.estimate.contact_samples)]]);
      html += `<p class="research-footnote">${escape(settingsText(plan.settings))}</p>`;
      html += table(['#', '기준', '대상', '중합도'], plan.items.map(i => [i.index + 1, `${escape(i.base.name)}<small class="mono">${escape(i.base.smiles)}</small>`, `${escape(i.screen.name)}<small class="mono">${escape(i.screen.smiles)}</small>`, `${fmt(i.base.n)} / ${fmt(i.screen.n)}`]));
    } else {
      html += features([['종류', '작업 묶음'], ['작업 수', fmt(plan.estimate.jobs), 'MD 줄에서 차례로'], ['공통 설정', fmt(Object.keys(plan.common).length)]]);
      html += table(['작업', '줄', '개별 설정', '원자 수'], plan.items.map(i => [escape(i.name), i.line, `<span class="mono">${escape(Object.entries(i.fields).map(([k, v]) => `${k}=${v}`).join(' · ') || '공통 설정만')}</span>`, fmt(i.estimate?.total_atoms)]));
      const notes = [...new Set(plan.items.flatMap(i => i.warnings || []))];
      html += warnings(notes);
    }
    return html + warnings(plan.warnings);
  }
  function actions(record) {
    const buttons = [];
    if (['queued', 'running', 'submitting'].includes(record.status) || (record.kind === 'jobs' && record.progress === 'running')) buttons.push(`<button class="secondary-button" type="button" data-sheet-action="cancel">취소</button>`);
    if (['interrupted', 'cancelled'].includes(record.status)) buttons.push(`<button class="secondary-button" type="button" data-sheet-action="resume">남은 것 이어서 실행</button>`);
    if (record.csv_url) buttons.push(`<a class="secondary-button" href="${escape(record.csv_url)}" download>결과 CSV</a>`);
    buttons.push(`<button class="text-button" type="button" data-sheet-load="${record.id}">실행표 다시 불러오기</button>`);
    return `<div class="research-actions">${buttons.join('')}</div>`;
  }
  function recordHTML(record) {
    const counts = Object.entries(record.counts || {}).map(([s, n]) => `${statusLabels[s] || s} ${n}`).join(' · ');
    let html = `<h3 class="research-subtitle">${escape(record.name)} <span class="research-tag ${record.status === 'failed' ? 'amber' : ''}">${label(record.kind === 'jobs' && record.status === 'submitted' ? (record.progress === 'running' ? 'running' : 'finished') : record.status)}</span></h3>`;
    html += `<p class="research-footnote">${escape(counts)} · 실행표 SHA-256 <span class="mono">${escape(record.sheet_sha256.slice(0, 16))}…</span>${record.error ? ` · ${escape(record.error)}` : ''}</p>`;
    if (record.kind === 'blends') {
      const done = record.items.filter(i => i.status === 'completed'), rank = new Map(record.ranking.map(r => [r.index, r.rank]));
      const reference = done[0]?.result.chi_reference_temperature_kelvin;
      html += features([['완료', `${fmt(done.length)} / ${fmt(record.items.length)}`], ['기준 온도', reference ? `${fmt(reference)} K` : '—', '298.15 K에 가장 가까운 온도'], ['가장 낮은 χ', record.ranking[0] ? `${escape(record.ranking[0].screen)}` : '—', record.ranking[0] ? `${record.ranking[0].base} 기준` : '']]);
      const ordered = [...record.items].sort((a, b) => (rank.get(a.index) ?? 1e9) - (rank.get(b.index) ?? 1e9) || a.index - b.index);
      html += table(['순위', '기준', '대상', 'χ (기준 온도)', 'A + B/T', '임계 χ', '상 거동', '상태'], ordered.map(i => {
        const r = i.result;
        return [rank.get(i.index) ?? '—', escape(i.base.name), escape(i.screen.name), r ? `${fmt(r.chi_reference)} <small>± ${fmt(r.bootstrap_std)}</small>` : '—',
          r ? `${fmt(r.A)} + ${fmt(r.B_kelvin)}/T` : '—', r ? fmt(r.critical_chi) : '—',
          r ? `${escape(r.phase_kind || '')}${Number.isFinite(r.critical_temperature_kelvin) ? `<small>Tc ${fmt(r.critical_temperature_kelvin)} K</small>` : ''}` : '—',
          `${label(i.status)}${i.error ? `<small>${escape(i.error)}</small>` : ''}`];
      }));
      html += `<p class="research-footnote">${escape(settingsText(record.settings))}. χ가 낮을수록 섞이는 쪽이지만 섞임을 확정하지 않습니다.</p>`;
    } else {
      html += table(['작업', '상태', '작업 ID'], record.items.map(i => [escape(i.name), `${label(i.status)}${i.error ? `<small>${escape(i.error)}</small>` : ''}`,
        i.status === 'not_created' ? '—' : `<button class="text-button mono" type="button" data-sheet-job="${i.job_id}">${i.job_id.slice(0, 8)} 열기</button>`]));
      html += '<p class="research-footnote">완료(completed)는 경로가 끝까지 돌았다는 뜻이며 과학적 타당성을 보증하지 않습니다. 결과는 각 작업에서 확인하세요.</p>';
    }
    return html + actions(record) + warnings(record.warnings);
  }
  function active(record) {
    return ['queued', 'running', 'submitting'].includes(record.status) || (record.kind === 'jobs' && record.progress === 'running');
  }
  async function show(identifier) {
    clearTimeout(timer);
    current = identifier;
    const record = await api(`/api/research/sheets/${identifier}`);
    if (current !== identifier) return;
    $('#sheet-result').innerHTML = recordHTML(record);
    if (active(record) && state.view === 'sheets') timer = setTimeout(() => show(identifier).catch(error => toast(error.message, true)), 2000);
    else refreshHistory();
  }
  async function refreshHistory() {
    const { records } = await api('/api/research/sheets');
    $('#sheet-history').innerHTML = records.length ? table(['이름', '종류', '상태', '항목', '만든 시각'], records.map(r => [
      `<button class="text-button" type="button" data-sheet-open="${r.id}">${escape(r.name)}</button>`, r.kind === 'blends' ? 'Blends χ' : '작업 묶음', label(r.status), fmt(r.items),
      escape(new Date(r.created_at).toLocaleString('ko-KR'))])) : '<p class="research-footnote">아직 실행한 실행표가 없습니다.</p>';
  }
  async function loadTemplates() {
    if (templates) return;
    if (!templateRequest) templateRequest = api('/api/research/sheets/templates').then(result => {
      templates = result.templates;
      $('#sheet-templates').innerHTML = templates.map(t => `<button class="text-button" type="button" data-sheet-template="${escape(t.id)}">예제: ${escape(t.title)}</button>`).join('');
    }).finally(() => { templateRequest = null; });
    await templateRequest;
  }
  async function check(text = $('#sheet-text').value) {
    const result = $('#sheet-result');
    result.setAttribute('aria-busy', 'true');
    try {
      const plan = await api('/api/research/sheets/preview', { method: 'POST', body: JSON.stringify({ text }) });
      clearTimeout(timer);
      current = null;
      result.innerHTML = previewHTML(plan);
      return plan;
    } finally { result.removeAttribute('aria-busy'); }
  }

  $('#sheet-check').addEventListener('click', () => formAction(() => check()).catch(error => toast(error.message, true)));
  $('#sheet-download').addEventListener('click', () => {
    const text = $('#sheet-text').value;
    if (!text.trim()) return toast('예제를 고르거나 실행표를 입력하세요.', true);
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = 'PolymerLab-sheet.txt'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $('#sheet-form').addEventListener('submit', async event => {
    event.preventDefault();
    formAction(async () => {
      const text = $('#sheet-text').value;
      const plan = await check(text);
      if (!plan.valid) return;
      const count = plan.kind === 'blends' ? `Blends 조합 ${plan.items.length}개` : `작업 ${plan.items.length}개`;
      if (!confirm(`${count}를 실행합니다. 계속할까요?`)) return;
      const record = await api('/api/research/sheets', { method: 'POST', body: JSON.stringify({ text }) });
      toast('실행표를 시작했습니다.');
      await show(record.id);
    }).catch(error => toast(error.message, true));
  });
  document.addEventListener('click', async event => {
    const target = event.target.closest('[data-sheet-template],[data-sheet-line],[data-sheet-open],[data-sheet-load],[data-sheet-action],[data-sheet-job]');
    if (!target || !target.closest('#view-sheets')) return;
    if (actionPending) return;
    try {
      if (target.dataset.sheetTemplate) await formAction(async () => { $('#sheet-text').value = templates.find(t => t.id === target.dataset.sheetTemplate).text; await check(); });
      else if (target.dataset.sheetLine) selectLine(Number(target.dataset.sheetLine));
      else if (target.dataset.sheetOpen) await show(target.dataset.sheetOpen);
      else if (target.dataset.sheetLoad) {
        const response = await fetch(`/api/research/sheets/${target.dataset.sheetLoad}/text`);
        if (!response.ok) throw new Error('저장된 실행표를 불러올 수 없습니다.');
        $('#sheet-text').value = await response.text(); toast('실행표를 입력 칸에 불러왔습니다.');
      }
      else if (target.dataset.sheetJob) openJob(target.dataset.sheetJob);
      else if (current) { await api(`/api/research/sheets/${current}/${target.dataset.sheetAction}`, { method: 'POST' }); await show(current); }
    } catch (error) { toast(error.message, true); }
  });
  $('#sheet-refresh').addEventListener('click', () => refreshHistory().catch(error => toast(error.message, true)));
  function enterView() {
    Promise.all([loadTemplates(), refreshHistory()]).then(() => current && show(current)).catch(error => toast(error.message, true));
  }
  document.addEventListener('polymer:view', event => {
    if (event.detail !== 'sheets') { clearTimeout(timer); return; }
    enterView();
  });
  if (state.view === 'sheets') enterView();
})();
