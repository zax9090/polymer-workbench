'use strict';
(() => {
  const labels = {passed:'기준 충족',pending:'추가 확인',failed:'미충족',unknown:'미평가'};
  window.qualityHTML = quality => {
    if (!quality) return '<p class="research-footnote">아직 결과 근거를 평가하지 않았습니다.</p>';
    const checks = Array.isArray(quality.checks) ? quality.checks : [];
    const next = Array.isArray(quality.next_checks) ? quality.next_checks : [];
    const review = quality.conclusion === 'review_required';
    return `<section class="quality-summary" aria-label="결과 근거 요약"><div class="quality-verdict"><strong>${review?'사람 검토 필요':'결론 보류'}</strong><span>자동 검증 승인 없음</span></div><p class="research-footnote">${escape(quality.message || '근거별 상태를 확인하세요. 개별 기준 충족은 과학적 검증 완료를 뜻하지 않습니다.')}</p><div class="quality-checks">${checks.map(check => `<details class="quality-check ${['passed','pending','failed','unknown'].includes(check.status)?check.status:'unknown'}"><summary><span>${escape(check.title || check.key)}</span><strong>${escape(labels[check.status] || '미평가')}</strong></summary><p>${escape(check.detail || '')}</p></details>`).join('')}</div>${next.length?`<div class="quality-next"><h4>다음 결정</h4><ol>${next.slice(0,3).map(item=>`<li>${escape(item.message)}${Number.isFinite(item.additional_runs)?` <small>추가 계산 ${escape(item.additional_runs)}건</small>`:''}</li>`).join('')}</ol>${next.length>3?`<details><summary>다른 확인 사항 ${next.length-3}개</summary><ul>${next.slice(3).map(item=>`<li>${escape(item.message)}</li>`).join('')}</ul></details>`:''}</div>`:''}<p class="research-footnote">표시된 기준은 실행·근거의 상태입니다. 시뮬레이션 관측값과 실험 검증을 구분하세요. 추가 계산 수는 겹칠 수 있는 제안이며 자동 합산한 예산이나 실행 승인이 아닙니다.</p></section>`;
  };
})();
