import * as vscode from 'vscode';
import axios from 'axios';

// ─── Language map (30+ languages) ───────────────────────────────────────────
const EXT_TO_LANG: Record<string, string> = {
  py: 'python', js: 'javascript', ts: 'typescript',
  jsx: 'javascript', tsx: 'typescript', html: 'html',
  css: 'css', scss: 'css', sass: 'css',
  c: 'c', cpp: 'cpp', rs: 'rust', go: 'go',
  java: 'java', kt: 'kotlin', scala: 'scala', groovy: 'groovy',
  rb: 'ruby', php: 'php', pl: 'perl', lua: 'lua', sh: 'bash',
  r: 'r', jl: 'julia', cs: 'csharp', fs: 'fsharp',
  swift: 'swift', dart: 'dart', ex: 'elixir', exs: 'elixir',
  hs: 'haskell', clj: 'clojure', sql: 'sql',
  yml: 'yaml', yaml: 'yaml', json: 'json',
};

// ─── Globals ─────────────────────────────────────────────────────────────────
let criticalDeco:   vscode.TextEditorDecorationType;
let warningDeco:    vscode.TextEditorDecorationType;
let suggestionDeco: vscode.TextEditorDecorationType;
let statusBarItem:  vscode.StatusBarItem;
let currentPanel:   vscode.WebviewPanel | undefined;

// ← NEW: remember which document/column was reviewed so "jump to line"
//   can re-focus it even after the webview has stolen editor focus.
let reviewedDocument:   vscode.TextDocument | undefined;
let reviewedViewColumn: vscode.ViewColumn   | undefined;

function getApiUrl(): string {
  return vscode.workspace
    .getConfiguration('buglens')
    .get<string>('apiUrl', 'http://localhost:8000');
}

// ─── Decorations ─────────────────────────────────────────────────────────────
function createDecorations() {
  criticalDeco = vscode.window.createTextEditorDecorationType({
    backgroundColor: 'rgba(239,68,68,0.12)',
    borderWidth: '0 0 1px 0',
    borderStyle: 'solid',
    borderColor: 'rgba(239,68,68,0.6)',
    overviewRulerColor: '#ef4444',
    overviewRulerLane: vscode.OverviewRulerLane.Right,
    light: { backgroundColor: 'rgba(220,38,38,0.08)' },
  });
  warningDeco = vscode.window.createTextEditorDecorationType({
    backgroundColor: 'rgba(245,158,11,0.10)',
    borderWidth: '0 0 1px 0',
    borderStyle: 'solid',
    borderColor: 'rgba(245,158,11,0.5)',
    overviewRulerColor: '#f59e0b',
    overviewRulerLane: vscode.OverviewRulerLane.Right,
  });
  suggestionDeco = vscode.window.createTextEditorDecorationType({
    backgroundColor: 'rgba(129,140,248,0.08)',
    borderWidth: '0 0 1px 0',
    borderStyle: 'solid',
    borderColor: 'rgba(129,140,248,0.4)',
    overviewRulerColor: '#818cf8',
    overviewRulerLane: vscode.OverviewRulerLane.Right,
  });
}

function clearDecorations(editor: vscode.TextEditor) {
  editor.setDecorations(criticalDeco,   []);
  editor.setDecorations(warningDeco,    []);
  editor.setDecorations(suggestionDeco, []);
}

function applyDecorations(editor: vscode.TextEditor, issues: any[]) {
  const showHighlights = vscode.workspace
    .getConfiguration('buglens')
    .get<boolean>('showInlineHighlights', true);
  if (!showHighlights) { return; }

  const critical:   vscode.DecorationOptions[] = [];
  const warning:    vscode.DecorationOptions[] = [];
  const suggestion: vscode.DecorationOptions[] = [];

  for (const issue of issues) {
    if (!issue.line_number) { continue; }
    const lineIdx = issue.line_number - 1;
    if (lineIdx < 0 || lineIdx >= editor.document.lineCount) { continue; }

    const line  = editor.document.lineAt(lineIdx);
    const range = new vscode.Range(line.range.start, line.range.end);
    const hoverMsg = new vscode.MarkdownString(
      `**BugLens [${issue.severity.toUpperCase()}]** — ${issue.title}\n\n` +
      `${issue.description}\n\n` +
      `**Fix:** \`${issue.fix}\``
    );
    hoverMsg.isTrusted = true;
    const deco: vscode.DecorationOptions = { range, hoverMessage: hoverMsg };

    if (issue.severity === 'critical')     { critical.push(deco); }
    else if (issue.severity === 'warning') { warning.push(deco); }
    else                                   { suggestion.push(deco); }
  }

  editor.setDecorations(criticalDeco,   critical);
  editor.setDecorations(warningDeco,    warning);
  editor.setDecorations(suggestionDeco, suggestion);
}

// ─── Status bar ───────────────────────────────────────────────────────────────
function updateStatusBar(score: number, issueCount: number) {
  const icon = score >= 80 ? '$(check)' : score >= 60 ? '$(warning)' : '$(error)';
  statusBarItem.text    = `${icon} BugLens: ${score}/100  (${issueCount} issues)`;
  statusBarItem.tooltip = `BugLens — Score: ${score}/100, Issues: ${issueCount}. Click to review again.`;
  statusBarItem.color   = score >= 80 ? '#22c55e' : score >= 60 ? '#f59e0b' : '#ef4444';
  statusBarItem.show();
}

// ─── Jump to line (FIXED) ──────────────────────────────────────────────────────
// The old handler used `vscode.window.activeTextEditor`, which is `undefined`
// the moment the webview panel has focus (a webview is not a text editor).
// That's exactly the state you're in when you click a line tag, so the old
// code silently did nothing. This version explicitly re-opens/focuses the
// document that was actually reviewed, in its original view column, then
// reveals + selects the target line.
async function jumpToLine(lineNumber: number) {
  if (!reviewedDocument) {
    vscode.window.showWarningMessage('BugLens: No reviewed file to jump to.');
    return;
  }

  let targetEditor: vscode.TextEditor;
  try {
    targetEditor = await vscode.window.showTextDocument(reviewedDocument, {
      viewColumn: reviewedViewColumn ?? vscode.ViewColumn.One,
      preserveFocus: false,
      preview: false,
    });
  } catch {
    vscode.window.showWarningMessage('BugLens: Could not reopen the reviewed file.');
    return;
  }

  const lineIdx = Math.min(
    Math.max(0, lineNumber - 1),
    Math.max(0, targetEditor.document.lineCount - 1)
  );
  const lineText = targetEditor.document.lineAt(lineIdx).text;
  const range = new vscode.Range(lineIdx, 0, lineIdx, lineText.length);

  targetEditor.revealRange(range, vscode.TextEditorRevealType.InCenter);
  targetEditor.selection = new vscode.Selection(range.start, range.end);
}

// ─── Main review function ─────────────────────────────────────────────────────
async function runReview(editor: vscode.TextEditor) {
  // ← NEW: remember what we're reviewing, so jumpToLine can find it later
  //   even after the webview steals focus.
  reviewedDocument   = editor.document;
  reviewedViewColumn = editor.viewColumn ?? vscode.ViewColumn.One;

  const selection = editor.selection;
  const code = selection.isEmpty
    ? editor.document.getText()
    : editor.document.getText(selection);

  if (code.length > 150_000) {
  vscode.window.showWarningMessage('BugLens: File too large (>150k chars). Select a specific function or class to review.');
  return;
  }

  if (code.trim().length < 10) {
    vscode.window.showWarningMessage('BugLens: Not enough code to review.');
    return;
  }

  const filename = editor.document.fileName.split(/[\\/]/).pop() || '';
  const ext      = filename.split('.').pop()?.toLowerCase() || '';
  const language = EXT_TO_LANG[ext] || ext || 'plaintext';

  if (currentPanel) {
    currentPanel.reveal(vscode.ViewColumn.Beside);
  } else {
    currentPanel = vscode.window.createWebviewPanel(
      'buglens',
      `BugLens — ${filename}`,
      vscode.ViewColumn.Beside,
      { enableScripts: true }
    );
    currentPanel.onDidDispose(() => { currentPanel = undefined; });

    // ← Register message handler ONCE when panel is first created
    currentPanel.webview.onDidReceiveMessage(async msg => {
      if (msg.command === 'copyFix') {
        vscode.env.clipboard.writeText(msg.text);
        vscode.window.showInformationMessage('BugLens: Fix copied to clipboard!');
      }
      if (msg.command === 'jumpToLine') {
        await jumpToLine(msg.line);
      }
    });
  }
  currentPanel.title        = `BugLens — ${filename}`;
  currentPanel.webview.html = getLoadingHtml(filename);

  clearDecorations(editor);

  try {
    const { data } = await axios.post(
      `${getApiUrl()}/api/v1/review`,
      { code, language, filename },
      { timeout: 60000  }
    );

    applyDecorations(editor, data.issues ?? []);
    updateStatusBar(data.score, data.issues?.length ?? 0);

    currentPanel.webview.html = getReviewHtml(data, filename, code, language);

  } catch (err: any) {
    const status = err.response?.status;
    const detail = err.response?.data?.detail;

    let msg = 'BugLens: Something went wrong.';
    if (err.code === 'ECONNREFUSED' || err.code === 'ERR_NETWORK') {
      msg = 'BugLens: Cannot reach backend. Is the server running on port 8000?';
    } else if (err.code === 'ECONNABORTED') {
      msg = 'BugLens: Request timed out. The server took too long to respond.';
    } else if (status === 422) {
      const detailMsg = typeof detail === 'string' ? detail : JSON.stringify(detail);
      msg = 'BugLens: Invalid request — ' + (detailMsg ?? 'check your code and language.');
    } else if (status === 400) {
      msg = 'BugLens: ' + (detail ?? 'Bad request.');
    } else if (status === 429) {
      msg = 'BugLens: Rate limit hit. Please wait a moment and try again.';
    } else if (status === 500) {
      msg = 'BugLens: Server error. Check the backend logs for details.';
    }

    vscode.window.showErrorMessage(msg);
    if (currentPanel) {
      currentPanel.webview.html = getErrorHtml(msg, filename);
    }
  }
}

// ─── Activate ─────────────────────────────────────────────────────────────────
export function activate(context: vscode.ExtensionContext) {
  createDecorations();

  statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right, 100
  );
  statusBarItem.command = 'buglens.reviewCode';
  statusBarItem.text    = '$(bug) BugLens';
  statusBarItem.tooltip = 'Click to run BugLens code review';
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  const reviewCmd = vscode.commands.registerCommand(
    'buglens.reviewCode',
    async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage('BugLens: Open a file to review.');
        return;
      }
      await runReview(editor);
    }
  );
  context.subscriptions.push(reviewCmd);

  const saveWatcher = vscode.workspace.onDidSaveTextDocument(async doc => {
    const enabled = vscode.workspace
      .getConfiguration('buglens')
      .get<boolean>('reviewOnSave', false);
    if (!enabled) { return; }
    const editor = vscode.window.visibleTextEditors.find(e => e.document === doc);
    if (editor) { await runReview(editor); }
  });
  context.subscriptions.push(saveWatcher);

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(editor => {
      if (editor) { clearDecorations(editor); }
    })
  );
}

export function deactivate() {
  criticalDeco?.dispose();
  warningDeco?.dispose();
  suggestionDeco?.dispose();
  statusBarItem?.dispose();
}

// ─── HTML helpers ─────────────────────────────────────────────────────────────

function getLoadingHtml(filename: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
  <style>
    body {
      background:#0f0f1a; color:#cdd6f4;
      font-family:'Segoe UI',sans-serif;
      display:flex; flex-direction:column; align-items:center;
      justify-content:center; height:100vh; margin:0;
    }
    .spinner {
      width:48px; height:48px;
      border:4px solid #1e1e2e;
      border-top:4px solid #6366f1;
      border-radius:50%;
      animation:spin 0.8s linear infinite;
      margin-bottom:20px;
    }
    @keyframes spin { to { transform:rotate(360deg); } }
    .label { font-size:14px; color:#6b7280; }
    .file  { font-size:12px; color:#4b5563; margin-top:6px; }
  </style></head><body>
  <div class="spinner"></div>
  <div class="label">🔍 Analyzing your code...</div>
  <div class="file">${filename}</div>
  </body></html>`;
}

function getErrorHtml(message: string, filename: string): string {
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
  <style>
    body {
      background:#0f0f1a; color:#cdd6f4;
      font-family:'Segoe UI',sans-serif;
      display:flex; flex-direction:column; align-items:center;
      justify-content:center; height:100vh; margin:0;
      text-align:center; padding:24px;
    }
    .icon { font-size:40px; margin-bottom:16px; }
    .msg  { font-size:14px; color:#f87171; max-width:400px; line-height:1.6; }
    .file { font-size:12px; color:#4b5563; margin-top:8px; }
  </style></head><body>
  <div class="icon">❌</div>
  <div class="msg">${message}</div>
  <div class="file">${filename}</div>
  </body></html>`;
}

function getScoreColor(score: number): string {
  if (score >= 80) { return '#22c55e'; }
  if (score >= 60) { return '#f59e0b'; }
  return '#ef4444';
}

function getScoreLabel(score: number): string {
  if (score >= 90) { return 'Excellent'; }
  if (score >= 80) { return 'Good'; }
  if (score >= 60) { return 'Needs Work'; }
  if (score >= 40) { return 'Poor'; }
  return 'Critical';
}

// ─── Safe HTML escaping for data attributes ───────────────────────────────────
function escapeHtml(str: string): string {
  return str
    .replace(/&/g,  '&amp;')
    .replace(/"/g,  '&quot;')
    .replace(/'/g,  '&#39;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;');
}

// ─── Main HTML ────────────────────────────────────────────────────────────────
function getReviewHtml(
  review:   any,
  filename: string,
  code:     string = '',
  language: string = ''
): string {
  const scoreColor = getScoreColor(review.score);
  const scoreLabel = getScoreLabel(review.score);
  const apiUrl     = getApiUrl();

  const sevColor: Record<string, string> = {
    critical:   '#ef4444',
    warning:    '#f59e0b',
    suggestion: '#818cf8',
  };
  const sevIcon: Record<string, string> = {
    critical:   '🔴',
    warning:    '🟡',
    suggestion: '🔵',
  };

  const issues: any[] = review.issues ?? [];
  const critical   = issues.filter(i => i.severity === 'critical').length;
  const warning    = issues.filter(i => i.severity === 'warning').length;
  const suggestion = issues.filter(i => i.severity === 'suggestion').length;

  const issuesHtml = issues.length
    ? issues.map((issue, idx) => `
      <div class="issue-card" style="border-left-color:${sevColor[issue.severity] ?? '#555'}">
        <div class="issue-header">
          <span>${sevIcon[issue.severity] ?? '⚪'}</span>
          <span class="sev-badge" style="color:${sevColor[issue.severity]};border-color:${sevColor[issue.severity]}44">
            ${issue.severity.toUpperCase()}
          </span>
          <span class="issue-title">${issue.title}</span>
        </div>
        ${issue.line_number ? `
          <div class="line-tag" onclick="jumpToLine(${issue.line_number})">
            📍 Line ${issue.line_number}
            <span class="jump-hint">click to jump</span>
          </div>` : ''}
        <div class="issue-desc">${issue.description}</div>
        <div class="fix-row">
          <div class="fix-box" id="fix-${idx}">${issue.fix}</div>
          <button class="copy-btn" onclick="copyFix('fix-${idx}')">Copy Fix</button>
        </div>
      </div>`).join('')
    : `<div class="no-issues">✅ No issues found — great code!</div>`;

  const positivesHtml = review.positive_aspects?.length
    ? `<div class="section">
        <div class="section-title">✨ What's Good</div>
        <ul class="positives">
          ${(review.positive_aspects as string[]).map(p => `<li>${p}</li>`).join('')}
        </ul>
       </div>` : '';

  const refactoredHtml = review.refactored_code
    ? `<div class="section">
        <div class="section-title">🔧 Refactored Version</div>
        <pre class="code-block">${review.refactored_code
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')}</pre>
       </div>` : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src ${apiUrl};">
  <style>
    * { box-sizing:border-box; margin:0; padding:0; }
    body {
      font-family:'Segoe UI',system-ui,sans-serif;
      background:#0f0f1a; color:#cdd6f4;
      padding:20px; line-height:1.6; font-size:13px;
    }
    .header {
      display:flex; align-items:center; gap:10px;
      padding-bottom:14px; margin-bottom:16px;
      border-bottom:1px solid #1e1e2e;
    }
    .header-title { font-size:16px; font-weight:600; color:#e2e8f0; }
    .header-file  { font-size:11px; color:#6b7280; margin-top:2px; }
    .score-card {
      background:#13131f; border:1px solid #1e1e2e;
      border-radius:10px; padding:14px 18px; margin-bottom:14px;
      display:flex; align-items:center; gap:16px;
    }
    .score-block { text-align:center; flex-shrink:0; }
    .score-num   { font-size:40px; font-weight:700; color:${scoreColor}; line-height:1; }
    .score-den   { font-size:12px; color:#6b7280; }
    .score-lbl   {
      font-size:10px; font-weight:600; color:${scoreColor};
      text-transform:uppercase; letter-spacing:0.06em; margin-top:3px;
    }
    .divider { width:1px; height:56px; background:#1e1e2e; flex-shrink:0; }
    .summary { font-size:12px; color:#94a3b8; line-height:1.65; }
    .stats { display:flex; gap:8px; margin-bottom:16px; }
    .stat  {
      flex:1; background:#13131f; border:1px solid #1e1e2e;
      border-radius:8px; padding:8px; text-align:center;
    }
    .stat-n { font-size:20px; font-weight:700; }
    .stat-l {
      font-size:9px; color:#6b7280;
      text-transform:uppercase; letter-spacing:0.05em; margin-top:1px;
    }
    .section       { margin-bottom:20px; }
    .section-title {
      font-size:11px; font-weight:600; color:#6b7280;
      text-transform:uppercase; letter-spacing:0.07em; margin-bottom:8px;
    }
    .issue-card {
      background:#13131f; border:1px solid #1e1e2e;
      border-left:3px solid; border-radius:8px;
      padding:11px 13px; margin-bottom:8px;
    }
    .issue-header {
      display:flex; align-items:center; flex-wrap:wrap;
      gap:6px; margin-bottom:5px;
    }
    .sev-badge {
      font-size:9px; padding:1px 7px; border-radius:20px;
      border:1px solid; font-weight:600; letter-spacing:0.05em;
    }
    .issue-title { font-size:13px; font-weight:500; color:#e2e8f0; }
    .line-tag {
      font-size:11px; color:#6366f1; cursor:pointer;
      margin-bottom:5px; display:inline-flex; align-items:center; gap:4px;
    }
    .line-tag:hover { text-decoration:underline; }
    .jump-hint  { font-size:9px; color:#4b5563; }
    .issue-desc { font-size:12px; color:#94a3b8; margin-bottom:8px; line-height:1.55; }
    .fix-row { display:flex; gap:8px; align-items:flex-start; }
    .fix-box {
      flex:1; background:#0d1117; border-radius:6px; padding:7px 10px;
      font-family:'Cascadia Code','Fira Code',monospace;
      font-size:11px; color:#86efac; line-height:1.5;
      border:1px solid #1e2d1e; word-break:break-word;
    }
    .copy-btn {
      flex-shrink:0; background:#1e2d1e; color:#86efac;
      border:1px solid #166534; border-radius:6px;
      padding:5px 10px; font-size:11px; cursor:pointer;
      transition:background 0.15s;
    }
    .copy-btn:hover { background:#166534; }
    .no-issues {
      background:#0d2818; border:1px solid #166534;
      border-radius:8px; padding:14px;
      font-size:13px; color:#86efac; text-align:center;
    }
    .positives {
      background:#13131f; border:1px solid #1e1e2e;
      border-radius:8px; padding:10px 12px 10px 28px;
      font-size:12px; color:#94a3b8;
    }
    .positives li { margin-bottom:4px; }
    .code-block {
      background:#0d1117; border:1px solid #1e1e2e;
      border-radius:8px; padding:12px;
      font-family:'Cascadia Code','Fira Code',monospace;
      font-size:11px; color:#cdd6f4;
      overflow-x:auto; white-space:pre; line-height:1.6;
    }
    #toast {
      position:fixed; bottom:20px; right:20px;
      background:#166534; color:#86efac;
      padding:8px 14px; border-radius:8px;
      font-size:12px; opacity:0; transition:opacity 0.3s;
      pointer-events:none;
    }
    #toast.show { opacity:1; }
    /* ── Chat ── */
    .chat-section {
      margin-top:24px; border-top:1px solid #1e1e2e; padding-top:16px;
    }
    .chat-messages {
      min-height:60px; max-height:300px; overflow-y:auto;
      margin-bottom:10px; display:flex; flex-direction:column; gap:8px;
    }
    .chat-hint { font-size:11px; color:#4b5563; font-style:italic; padding:4px 0; }
    .chat-msg  { padding:8px 12px; border-radius:8px; font-size:12px; line-height:1.6; }
    .chat-msg.user {
      background:#1e1e2e; color:#cdd6f4; align-self:flex-end;
      max-width:85%; border:1px solid #313244;
    }
    .chat-msg.bot {
      background:#13131f; color:#cdd6f4; align-self:flex-start;
      max-width:95%; border:1px solid #1e1e2e;
    }
    .chat-msg.bot.loading { color:#6b7280; }
    .chat-input-row { display:flex; gap:8px; }
    .chat-input {
      flex:1; background:#13131f; border:1px solid #313244;
      border-radius:8px; padding:8px 12px; font-size:12px;
      color:#cdd6f4; outline:none; font-family:'Segoe UI',sans-serif;
    }
    .chat-input:focus { border-color:#6366f1; }
    .chat-send {
      background:#6366f1; color:#fff; border:none; border-radius:8px;
      padding:8px 16px; font-size:12px; cursor:pointer;
    }
    .chat-send:hover { background:#4f46e5; }
  </style>
</head>
<body>

  <div class="header">
    <span style="font-size:20px">🔍</span>
    <div>
      <div class="header-title">BugLens Review</div>
      <div class="header-file">${filename}</div>
    </div>
  </div>

  <div class="score-card">
    <div class="score-block">
      <div class="score-num">${review.score}</div>
      <div class="score-den">/100</div>
      <div class="score-lbl">${scoreLabel}</div>
    </div>
    <div class="divider"></div>
    <div class="summary">${review.summary}</div>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-n" style="color:#ef4444">${critical}</div>
      <div class="stat-l">Critical</div>
    </div>
    <div class="stat">
      <div class="stat-n" style="color:#f59e0b">${warning}</div>
      <div class="stat-l">Warnings</div>
    </div>
    <div class="stat">
      <div class="stat-n" style="color:#818cf8">${suggestion}</div>
      <div class="stat-l">Suggestions</div>
    </div>
    <div class="stat">
      <div class="stat-n">${issues.length}</div>
      <div class="stat-l">Total</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Issues Found</div>
    ${issuesHtml}
  </div>

  ${positivesHtml}
  ${refactoredHtml}

  <!-- ── Chat section ── -->
  <div class="chat-section">
    <div class="section-title">💬 Ask BugLens about this code</div>
    <div class="chat-messages" id="chatMessages">
      <div class="chat-hint">
        Try: "Explain the SQL injection" · "How do I fix line 3?" · "What's the impact of this bug?"
      </div>
    </div>
    <div class="chat-input-row">
      <input
        type="text"
        id="chatInput"
        class="chat-input"
        placeholder="Ask anything about this code..."
        onkeydown="if(event.key==='Enter') sendChat()"
      />
      <button class="chat-send" onclick="sendChat()">Ask →</button>
    </div>
  </div>

  <!-- ── Safe data store — immune to </script> tags and special chars ── -->
  <div id="__bugdata__"
       data-code="${escapeHtml(code)}"
       data-lang="${escapeHtml(language)}"
       data-filename="${escapeHtml(filename)}"
       data-summary="${escapeHtml(review.summary ?? '')}"
       data-apiurl="${escapeHtml(apiUrl)}"
       style="display:none"></div>

  <div id="toast">✅ Copied to clipboard!</div>

  <script>
    const vscode    = acquireVsCodeApi();
    const __d       = document.getElementById('__bugdata__');
    const CODE      = __d.getAttribute('data-code');
    const LANG      = __d.getAttribute('data-lang');
    const FILENAME  = __d.getAttribute('data-filename');
    const SUMMARY   = __d.getAttribute('data-summary');
    const API_URL   = __d.getAttribute('data-apiurl');
    let   chatHistory = [];

    function copyFix(id) {
      const text = document.getElementById(id)?.innerText ?? '';
      vscode.postMessage({ command: 'copyFix', text });
      const toast = document.getElementById('toast');
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2000);
    }

    function jumpToLine(line) {
      vscode.postMessage({ command: 'jumpToLine', line });
    }

    async function sendChat() {
      const input    = document.getElementById('chatInput');
      const question = input.value.trim();
      if (!question) { return; }

      input.value = '';
      appendMessage('user', question);

      const loadingId = 'loading-' + Date.now();
      appendMessage('bot loading', '🤔 Thinking...', loadingId);
      chatHistory.push({ role: 'user', content: question });

      try {
        const resp = await fetch(API_URL + '/api/v1/chat', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            code:           CODE,
            language:       LANG,
            filename:       FILENAME,
            review_summary: SUMMARY,
            messages:       chatHistory
          })
        });

        const data  = await resp.json();
        const reply = data.reply || 'Sorry, no response.';

        const el = document.getElementById(loadingId);
        if (el) { el.className = 'chat-msg bot'; el.textContent = reply; }

        chatHistory.push({ role: 'assistant', content: reply });

      } catch (e) {
        const el = document.getElementById(loadingId);
        if (el) {
          el.className   = 'chat-msg bot';
          el.textContent = 'Error reaching backend. Is the server running?';
        }
      }
    }

    function appendMessage(type, text, id) {
      const messages = document.getElementById('chatMessages');
      const div      = document.createElement('div');
      div.className  = 'chat-msg ' + type;
      div.textContent = text;
      if (id) { div.id = id; }
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
    }
  </script>
</body>
</html>`;
}