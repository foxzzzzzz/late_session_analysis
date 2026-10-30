/* Incremental progress shared by the dashboard and run detail page. */
(() => {
    const script = document.querySelector('script[src*="pipeline-progress.js"]');
    const delay = Number(script.dataset.pollSeconds) * 1000;
    const detail = document.querySelector('[data-pipeline-detail]');
    const recent = document.querySelector('[data-pipeline-recent]');
    const note = document.querySelector('[data-live-note]');
    let cursor = detail ? Number(detail.dataset.cursor) : 0;

    function element(tag, text, className) {
        const node = document.createElement(tag);
        node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function renderStatus(node, status) {
        node.replaceChildren(element('span', '', `status-dot ${status}`),
                             document.createTextNode(status));
    }

    async function poll() {
        let again = true;
        let wait = delay;
        try {
            const url = detail ? `/pipeline/status/${detail.dataset.pipelineDetail}?after=${cursor}`
                               : '/api/recent-runs';
            const response = await fetch(url, {cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (recent) {
                const rows = data.runs.map(run => {
                    const row = element('tr', '');
                    const date = element('td', '');
                    const link = element('a', run.date);
                    link.href = `/pipeline/${run.id}`;
                    date.append(link);
                    const status = element('td', '');
                    renderStatus(status, run.status);
                    row.append(date, status, element('td', run.regime));
                    for (const stage of ['S0', 'S1', 'S2', 'S3', 'S4']) {
                        row.append(element('td', run.stages[stage] ?? '–'));
                    }
                    return row;
                });
                if (rows.length) recent.replaceChildren(...rows);
                note.textContent = '已连接 · 每阶段完成后更新';
            }
            if (detail) {
                renderStatus(detail.querySelector('[data-run-status]'), data.status);
                detail.querySelector('[data-run-regime]').textContent = data.regime;
                detail.querySelector('[data-run-finished]').textContent = data.finished_at ?? '–';
                for (const node of detail.querySelectorAll('[data-stage]')) {
                    node.textContent = data.stages[node.dataset.stage] ?? '–';
                }
                const terminal = detail.querySelector('[data-log-container]');
                const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 40;
                for (const log of data.logs) {
                    detail.querySelector('[data-log-empty]')?.remove();
                    const line = element('div', '', 'log-line');
                    line.append(element('span', log.timestamp, 'log-time'),
                                element('span', log.stage, `log-stage ${log.stage}`),
                                element('span', log.level, `log-level ${log.level}`),
                                element('span', log.message, 'log-msg'));
                    terminal.append(line);
                }
                if (atBottom) terminal.scrollTop = terminal.scrollHeight;
                detail.querySelector('[data-log-count]').textContent = `${data.total_logs} 条记录`;
                cursor = data.cursor;
                // 完成后仍需读完所有分页，防止漏掉末轮/异常日志。
                again = ['running', 'pending'].includes(data.status) ||
                        (data.logs.length && terminal.querySelectorAll('.log-line').length < data.total_logs);
                if (terminal.querySelectorAll('.log-line').length < data.total_logs) wait = 0;
                note.textContent = `${again ? '已连接' : '已结束'} · 最后进展 ${data.last_progress_at ?? '暂无'}`;
            }
        } catch (error) {
            note.textContent = '连接中断，正在重连；保留已有进度';
        }
        if (again) setTimeout(poll, wait);
    }
    if (detail || recent) poll();
})();
