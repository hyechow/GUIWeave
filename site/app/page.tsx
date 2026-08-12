const capabilities = [
  {
    index: "01",
    title: "Browser Agent",
    copy: "连接本地 Chrome，理解页面、规划任务并执行完整浏览器工作流。",
    tag: "Chrome · CDP",
  },
  {
    index: "02",
    title: "Android Agent",
    copy: "通过 ADB 操作真实设备或模拟器，保留截图、轨迹与可复现报告。",
    tag: "Android · ADB",
  },
  {
    index: "03",
    title: "Private Knowledge",
    copy: "将 PDF、Markdown 和文本手册提炼为应用知识；预览确认后才启用。",
    tag: "PDF → Knowledge",
  },
];

const tools = [
  "check_environment",
  "run_browser_task",
  "run_android_task",
  "get_run_result",
  "preview_knowledge_document",
  "get_knowledge_draft",
  "commit_knowledge_draft",
  "list_user_knowledge",
  "get_user_knowledge",
];

const flow = [
  ["01", "用户意图", "用自然语言描述一个有边界的 GUI 目标"],
  ["02", "Skill 路由", "识别平台、检查环境与安全确认"],
  ["03", "stdio MCP", "本机进程内传递工具调用，不开放网络端口"],
  ["04", "模型网关", "连接用户配置的模型服务，需要对应 API_KEY"],
  ["05", "Tool Agent", "观察、规划、操作并验证最终状态"],
  ["06", "可审计产物", "日志、截图、Trace、HTML Report 与 Replay"],
];

function ArrowIcon() {
  return <span aria-hidden="true">↗</span>;
}

export default function Home() {
  return (
    <main>
      <nav className="nav shell" aria-label="主导航">
        <a className="brand" href="#top" aria-label="GUIWeave 首页">
          <span className="brand-mark"><i /><i /><i /></span>
          <span>GUIWEAVE</span>
        </a>
        <div className="nav-links">
          <a href="#capabilities">能力</a>
          <a href="#architecture">架构</a>
          <a href="#knowledge">Knowledge</a>
        </div>
        <a className="nav-cta" href="#install">开发者预览 <ArrowIcon /></a>
      </nav>

      <section className="hero shell" id="top">
        <div className="hero-copy">
          <p className="eyebrow"><span /> macOS Developer Preview</p>
          <h1>让 AI 真正<br />使用你的界面。</h1>
          <p className="hero-lead">
            GUIWeave 把 <strong>Skill + 本地 stdio MCP</strong> 组合成一个可安装的
            GUI Agent 插件。在你的 Mac 上运行，理解浏览器与 Android 界面，完成任务，留下证据。
          </p>
          <div className="hero-actions">
            <a className="button primary" href="#install">查看安装方式 <ArrowIcon /></a>
            <a className="button secondary" href="#architecture">了解工作原理</a>
          </div>
          <div className="trust-row">
            <span><b>LOCAL RUNTIME</b> 执行环境在本机</span>
            <span><b>REVIEWED</b> 确认后写入</span>
            <span><b>TRACEABLE</b> 全程可追溯</span>
          </div>
        </div>

        <div className="agent-console" aria-label="GUIWeave 运行示意">
          <div className="console-top">
            <span className="console-title">tool-agent / live run</span>
            <span className="live"><i /> RUNNING</span>
          </div>
          <div className="viewport">
            <div className="fake-toolbar">
              <span className="dots">•••</span>
              <span className="address">localhost:12000 / orders</span>
              <span>⌘</span>
            </div>
            <div className="fake-app">
              <aside>
                <b>R</b>
                <i className="active" /><i /><i /><i /><i />
              </aside>
              <div className="fake-content">
                <div className="fake-heading"><span /><span /></div>
                <div className="metric-row"><i /><i /><i /></div>
                <div className="table-head" />
                {[0, 1, 2, 3].map((row) => <div className="table-row" key={row}><i /><i /><i /><b /></div>)}
                <div className="target-ring"><span>07</span></div>
              </div>
            </div>
          </div>
          <div className="console-log">
            <p><span>00:14.2</span> observation <b>Orders / 24 records</b></p>
            <p><span>00:15.0</span> action <b>open_order(id: #RT-2048)</b></p>
            <p><span>00:15.8</span> verify <strong>✓ target state reached</strong></p>
          </div>
          <div className="console-foot"><span>RUN ID — 20260812_0446</span><span>VIEW REPORT ↗</span></div>
        </div>
      </section>

      <section className="marquee" aria-label="支持能力">
        <div>TOOL AGENT <span>✦</span> BROWSER <span>✦</span> ANDROID <span>✦</span> KNOWLEDGE <span>✦</span> EVALS <span>✦</span> REPLAY</div>
      </section>

      <section className="section shell" id="capabilities">
        <header className="section-head">
          <p className="eyebrow"><span /> What it does</p>
          <h2>一个插件，三种核心能力。</h2>
          <p>从执行，到学习，再到验证——共用同一套本地运行时与安全边界。</p>
        </header>
        <div className="capability-grid">
          {capabilities.map((item) => (
            <article className="capability-card" key={item.index}>
              <div className={`card-visual visual-${item.index}`}>
                <span className="card-number">{item.index}</span>
                {item.index === "01" && <div className="browser-glyph"><i /><i /><i /><b /></div>}
                {item.index === "02" && <div className="phone-glyph"><span /><i>◎</i><b /></div>}
                {item.index === "03" && <div className="doc-glyph"><span>PDF</span><i>→</i><b>KN</b></div>}
              </div>
              <div className="card-copy">
                <span className="tag">{item.tag}</span>
                <h3>{item.title}</h3>
                <p>{item.copy}</p>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="architecture" id="architecture">
        <div className="shell architecture-inner">
          <div className="architecture-copy">
            <p className="eyebrow light"><span /> Local by design</p>
            <h2>运行时留在本机，<br />模型连接由你配置。</h2>
            <p>Codex 通过 stdio 启动本地 GUIWeave，无需另行部署公网 Agent 服务，也不会复制浏览器会话。Agent 推理并非离线运行：它会访问用户配置的模型网关，需要配置对应的 API_KEY，并受该网关的网络与数据策略约束。</p>
            <div className="architecture-note">
              <b>安全边界</b>
              <span>涉及发送、发布、购买、删除或账户设置时，执行前需要明确授权。</span>
            </div>
          </div>
          <div className="flow-list">
            {flow.map(([num, title, copy]) => (
              <div className="flow-item" key={num}>
                <span>{num}</span><div><b>{title}</b><p>{copy}</p></div><i>→</i>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="section knowledge shell" id="knowledge">
        <div className="knowledge-demo">
          <div className="file-card source-file">
            <span className="file-type">PDF</span>
            <div><b>Robo Team 使用说明书.pdf</b><small>72 pages · private document</small></div>
          </div>
          <div className="transform-arrow"><span>PREVIEW</span>→</div>
          <div className="knowledge-stack">
            <div className="file-card"><span>MD</span><b>_app.md</b></div>
            <div className="file-card"><span>MD</span><b>order_management.md</b></div>
            <div className="file-card"><span>MD</span><b>map_management.md</b></div>
            <div className="more-files">+ 10 focused sections</div>
          </div>
        </div>
        <div className="knowledge-copy">
          <p className="eyebrow"><span /> Document → Knowledge</p>
          <h2>让说明书变成<br />Agent 真正会用的知识。</h2>
          <p>上传应用手册后，系统提取导航、字段、工作流、状态含义和业务约束。凭据会先脱敏，文档中的指令不会被执行。</p>
          <ol>
            <li><b>01</b><span><strong>生成草稿</strong> — 原始文档只作为不可信数据读取</span></li>
            <li><b>02</b><span><strong>人工预览</strong> — 查看文件、警告与知识摘要</span></li>
            <li><b>03</b><span><strong>确认启用</strong> — 必须在后续消息中明确确认</span></li>
          </ol>
        </div>
      </section>

      <section className="tool-section shell">
        <div className="tool-heading">
          <p className="eyebrow"><span /> MCP surface</p>
          <h2>9 个清晰、可审查的工具。</h2>
        </div>
        <div className="tool-grid">
          {tools.map((tool, i) => <code key={tool}><span>{String(i + 1).padStart(2, "0")}</span>{tool}</code>)}
        </div>
      </section>

      <section className="install" id="install">
        <div className="shell install-inner">
          <div>
            <p className="eyebrow light"><span /> Get the preview</p>
            <h2>从你的 Mac 开始。</h2>
            <p>开发者预览版面向本地源码安装。保留 Tool Agent Master、WebArena、MobileWorld、日志、可视化与 Evals。使用前需选择模型提供商，配置模型网关地址与对应 API_KEY。</p>
          </div>
          <div className="install-card">
            <div className="terminal-bar"><span>INSTALL.sh</span><i>macOS 13+</i></div>
            <pre><span>$</span> git clone &lt;guiweave-repository&gt;{`\n`}<span>$</span> cd guiweave{`\n`}<span>$</span> uv sync{`\n`}<span>$</span> codex plugin marketplace add .{`\n`}<span>$</span> codex plugin add guiweave-automation@guiweave-dev</pre>
            <p>需要 Python 3.11+、uv、Codex，以及可访问的模型网关与对应 API_KEY。浏览器能力需要 Chrome；Android 能力需要 ADB。</p>
          </div>
        </div>
      </section>

      <footer className="shell">
        <a className="brand" href="#top"><span className="brand-mark"><i /><i /><i /></span><span>GUIWEAVE</span></a>
        <p>Local GUI automation for agents.</p>
        <span>MACOS DEVELOPER PREVIEW · 2026</span>
      </footer>
    </main>
  );
}
