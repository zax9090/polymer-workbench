'use strict';
(() => {
  // A report copies saved numbers and review flags; this page only creates, opens and uploads it.
  window.reportButton = (kind, id) => `<span class="report-action"><button type="button" class="secondary-button" data-report-kind="${escape(kind)}"${id ? ` data-report-id="${escape(id)}"` : ''}>보고서 만들기</button><span class="report-links" aria-live="polite"></span></span>`;
  const verified = record => `<span class="research-tag">Drive 확인됨 · 파일 ${Number(record.drive.files)}개</span>`;
  function links(record) {
    const drive = record.drive?.verified ? verified(record) : `<button type="button" class="secondary-button" data-report-upload="${escape(record.id)}">Drive에 올리기</button>`;
    const notes = record.warnings?.length ? `<small class="research-footnote">계산 주의사항 ${record.warnings.length}개가 보고서에 원문으로 들어 있습니다.</small>` : '';
    return `<a class="secondary-button" href="${escape(safeURL(record.view_url))}" target="_blank" rel="noopener">보고서 보기 ↗</a><a class="secondary-button" href="${escape(safeURL(record.download_url))}" download>보고서 ZIP ↓</a>${drive}${notes}`;
  }
  async function post(path, body) {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(typeof detail === 'string' ? detail : detail?.message || `서버 응답 오류 (${response.status})`);
    }
    return data;
  }
  document.addEventListener('click', async event => {
    const create = event.target.closest('[data-report-kind]'), upload = event.target.closest('[data-report-upload]');
    const button = create || upload;
    if (!button) return;
    const area = button.closest('.report-action')?.querySelector('.report-links');
    area?.querySelector('.error-message')?.remove();
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      if (create) {
        const record = await post('/api/research/reports', { kind: create.dataset.reportKind, id: create.dataset.reportId || null });
        area.innerHTML = links(record);
        create.textContent = '보고서 다시 만들기';
      } else {
        const record = await post(`/api/research/reports/${encodeURIComponent(upload.dataset.reportUpload)}/drive`, {});
        upload.outerHTML = verified(record);
      }
    } catch (error) {
      area?.insertAdjacentHTML('beforeend', `<p class="error-message">${escape(error.message)}</p>`);
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  });
})();
