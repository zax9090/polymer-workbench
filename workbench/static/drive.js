'use strict';
(() => {
  let configured = false, busy = false;
  const dialog = $('#drive-dialog');
  async function refresh() {
    try {
      const status = await api('/api/research/drive-status');
      configured = status.configured === true;
      $('#drive-status-label').textContent = configured ? 'Drive 설정됨' : 'Drive 설정 필요';
      $('#drive-connection').innerHTML = `<p>${escape(status.message)}</p>${status.setup_instructions && !configured ? `<details><summary>연결 설정</summary><p>${escape(status.setup_instructions)}</p><a href="/manual" target="_blank" rel="noopener">설명서 ↗</a></details>` : ''}`;
    } catch (error) {
      configured = false;
      $('#drive-status-label').textContent = 'Drive 확인 필요';
      $('#drive-connection').innerHTML = `<div class="error-message">${escape(error.message)}</div>`;
    }
    $('#drive-upload').disabled = !configured || busy;
    $('#drive-local').disabled = busy;
  }
  $('#drive-open').addEventListener('click', () => { dialog.showModal(); refresh(); });
  $('#drive-refresh').addEventListener('click', refresh);
  async function backup(local = false) {
    if (busy || (!local && !configured)) return;
    busy = true;
    $('#drive-upload').disabled = true;
    $('#drive-local').disabled = true;
    $('#drive-result').setAttribute('aria-busy', 'true');
    $('#drive-result').innerHTML = `<p role="status">${local ? '전체 백업 ZIP을 만들고 있습니다…' : '전체 백업을 만들고 Drive에 업로드합니다…'}</p>`;
    try {
      const response = await fetch(`/api/research/drive-backups${local ? '/local' : ''}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const result = await response.json();
      if (!response.ok) {
        const detail = typeof result.detail === 'string' ? result.detail : result.detail?.message || 'Drive 저장에 실패했습니다.';
        const local = result.download_url || result.detail?.download_url;
        $('#drive-result').innerHTML = `<div class="error-message" role="alert">${escape(detail)}</div>${local ? `<a href="${escape(safeURL(local))}" download>만들어진 로컬 백업 다운로드 ↓</a>` : ''}`;
      } else {
        if (local ? result.status !== 'local_ready' : result.status !== 'uploaded' || result.verified !== true) throw new Error('백업 완료 상태를 확인하지 못했습니다.');
        $('#drive-result').innerHTML = `<div class="success-message">${local ? '로컬 백업 ZIP 생성 완료 · Drive 업로드 전' : 'Drive 저장·검증 완료'}</div><p>${escape(result.filename)} · ${escape(sizeLabel(result.bytes))}</p>${result.destination ? `<p class="research-footnote">${escape(result.destination)}</p>` : ''}${result.download_url ? `<a href="${escape(safeURL(result.download_url))}" download>백업 ZIP 다운로드 ↓</a>` : ''}`;
      }
    } catch (error) { $('#drive-result').innerHTML = `<div class="error-message" role="alert">${escape(error.message)}</div>`; }
    finally { busy = false; $('#drive-upload').disabled = !configured; $('#drive-local').disabled = false; $('#drive-result').setAttribute('aria-busy', 'false'); }
  }
  $('#drive-upload').addEventListener('click', () => backup(false));
  $('#drive-local').addEventListener('click', () => backup(true));
  refresh();
})();
