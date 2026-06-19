export default {
  name: "NimdaApp",
  data() {
    return {
      legacyError: "",
    };
  },
  mounted() {
    this.mountLegacyShell();
  },
  methods: {
    async mountLegacyShell() {
      try {
        await import("/features/collection-detail/index.js?v=115");
        await import("/features/collection-info/index.js?v=115");
        await import("./legacy/shell.js?v=115");
      } catch (error) {
        this.legacyError = error && error.message ? error.message : String(error);
      }
    },
  },
  template: `
    <div class="nimda-app-shell">
      <header class="top">
        <div class="toolbar toolbar-row">
          <label class="theme-select-wrap">
            <span class="theme-select-label">界面风格</span>
            <span class="theme-select-ui">
              <select id="theme-select" class="theme-select" aria-label="界面风格">
                <option value="midnight">深蓝</option>
                <option value="paper">纸张浅色</option>
                <option value="forest">森林暗绿</option>
                <option value="rose">玫紫暖色</option>
                <option value="contrast">高对比</option>
                <option value="ocean">海洋青蓝</option>
                <option value="sunset">落日暖橙</option>
                <option value="slate">岩灰靛紫</option>
                <option value="sakura">樱花浅色</option>
              </select>
            </span>
          </label>
          <label class="theme-select-wrap">
            <span class="theme-select-label">界面字体</span>
            <span class="theme-select-ui">
              <select id="font-select" class="theme-select font-select" aria-label="界面字体">
                <option value="reference">参考网页</option>
                <option value="system">系统默认</option>
                <option value="yahei">微软雅黑</option>
                <option value="song">宋体</option>
                <option value="mono">等宽</option>
              </select>
            </span>
          </label>
        </div>
        <div id="status-line" class="status" aria-live="polite"></div>
      </header>

      <nav class="app-tabs nav" aria-label="页面">
        <button type="button" class="app-tab is-active" id="tab-collection-detail" data-tab="collection-detail">
          作品数据
        </button>
        <button type="button" class="app-tab" id="tab-collection-info" data-tab="collection-info">
          收集情况
        </button>
      </nav>

      <main id="collection-detail-view" class="feature-view">
        <section id="collection-detail-config-slot">
          <section id="config-panel" class="cfg-panel cfg-panel--in-feature" aria-label="服务端配置">
            <h2 class="cfg-title">当前配置（服务端）</h2>
            <div class="cfg-grid">
              <label>
                浏览配置文件
                <input id="cfg-used-path" type="text" readonly placeholder="（未找到配置文件）" />
              </label>
              <label>
                数据 DB（paths.filesystem_root）
                <input id="cfg-fs-root" type="text" readonly />
              </label>
              <label>
                保存备份目录
                <input id="cfg-history-root" type="text" readonly />
              </label>
              <label>
                索引媒体根目录
                <input id="cfg-link-media-root" type="text" readonly />
              </label>
              <label>
                索引输出目录
                <input id="cfg-link-shortcut-root" type="text" readonly />
              </label>
              <label>
                索引目录层级
                <input id="cfg-link-layout" type="text" readonly />
              </label>
              <label>
                索引链接命名
                <input id="cfg-link-name" type="text" readonly />
              </label>
            </div>
            <div class="cfg-actions">
              <button type="button" class="btn secondary" id="btn-reload-cfg">重新读取配置</button>
              <div class="yaml-file-block yaml-file-block--cfg-actions">
                <div class="yaml-file-head-row">
                  <label class="file-line">
                    <span class="btn secondary">选择 YAML 文件</span>
                    <input id="yaml-file" type="file" multiple accept=".yaml,.yml,text/yaml" />
                    <span id="file-meta" class="muted"></span>
                  </label>
                  <div class="db-catalog-anchor">
                    <button
                      type="button"
                      class="btn"
                      id="btn-load-default"
                      disabled
                      aria-expanded="false"
                      aria-haspopup="dialog"
                      aria-controls="db-catalog-popover"
                    >
                      加载DB数据
                    </button>
                    <div
                      id="db-catalog-popover"
                      class="db-catalog-popover"
                      role="dialog"
                      aria-modal="false"
                      aria-label="从 DB 选择数据并入表"
                      hidden
                    >
                      <div class="db-catalog-inner">
                        <h3 class="db-catalog-title">DB 数据文件</h3>
                        <div class="yaml-pick-toolbar db-catalog-toolbar">
                          <span class="yaml-pick-summary">共 <strong id="db-catalog-total">0</strong> 个</span>
                          <button type="button" class="btn secondary sm" id="btn-db-catalog-all">全选</button>
                          <button type="button" class="btn secondary sm" id="btn-db-catalog-none">全不选</button>
                        </div>
                        <div class="db-catalog-load-inline" role="group" aria-label="载入 DB 数据">
                          <select id="db-catalog-load-mode" class="db-catalog-load-select" aria-label="载入范围" disabled>
                            <option value="picked">仅勾选数据</option>
                            <option value="all">全部数据</option>
                          </select>
                          <button type="button" class="btn sm" id="btn-db-catalog-run" disabled>载入</button>
                        </div>
                        <ul id="db-catalog-list" class="yaml-pick-list db-catalog-list"></ul>
                      </div>
                    </div>
                  </div>
                  <button type="button" class="btn" id="btn-sheet-save" disabled>保存到 YAML</button>
                  <button type="button" class="btn secondary" id="btn-sheet-add-row" disabled>新增行</button>
                  <button type="button" class="btn secondary" id="btn-enum-editor" disabled>枚举</button>
                </div>
                <div id="yaml-pick-panel" class="yaml-pick-panel" hidden>
                  <div class="yaml-pick-toolbar">
                    <span class="yaml-pick-summary">已选 <strong id="yaml-pick-count">0</strong> 个</span>
                    <button type="button" class="btn secondary sm" id="btn-yaml-pick-all">全选</button>
                    <button type="button" class="btn secondary sm" id="btn-yaml-pick-none">全不选</button>
                    <button type="button" class="btn sm" id="btn-yaml-pick-load">载入勾选</button>
                  </div>
                  <ul id="yaml-pick-list" class="yaml-pick-list"></ul>
                </div>
                <div id="enum-editor-panel" class="enum-editor-panel" hidden>
                  <div id="enum-editor-body" class="enum-editor-body"></div>
                  <div class="enum-editor-actions">
                    <button type="button" class="btn sm" id="btn-enum-save">保存枚举</button>
                    <button type="button" class="btn secondary sm" id="btn-enum-close">关闭</button>
                  </div>
                </div>
              </div>
              <label class="edit-toggle edit-toggle--cfg-actions">
                <input id="chk-sheet-edit" type="checkbox" autocomplete="off" />
                <span>编辑模式</span>
              </label>
            </div>
          </section>
        </section>
        <nav class="collection-detail-subtabs" aria-label="作品数据子页签">
          <button type="button" class="collection-detail-subtab is-active" data-collection-detail-subtab="list">
            收集列表
          </button>
          <button type="button" class="collection-detail-subtab" data-collection-detail-subtab="index">
            索引目录
          </button>
          <button type="button" class="collection-detail-subtab" data-collection-detail-subtab="resource">
            资源库目录
          </button>
        </nav>
        <section id="collection-detail-list-panel" class="collection-detail-subpanel">
          <section id="viewport"></section>
        </section>
        <section id="collection-detail-index-panel" class="collection-detail-subpanel" hidden>
          <section id="collection-detail-link-index-slot"></section>
        </section>
        <section id="collection-detail-resource-panel" class="collection-detail-subpanel" hidden>
          <section id="collection-detail-resource-slot"></section>
        </section>
      </main>

      <main id="collection-info-view" class="collection-info-view collection-records-view" hidden></main>
      <p v-if="legacyError" class="status err">Vue 挂载旧功能失败：{{ legacyError }}</p>
      <footer class="foot muted" aria-hidden="true">&nbsp;</footer>
    </div>
  `,
};
