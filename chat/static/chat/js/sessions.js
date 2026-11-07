/* =========================================================================
   左栏：会话列表
   负责会话的加载、切换、新建、重命名、删除；打开会话的渲染交给 chat。
   ========================================================================= */

const sessions = (() => {
  const KEY = "active_session_id";
  const LEGACY_KEY = "session_id";   // 旧版本只存了这个

  let list = [];
  let activeId = null;

  const box = () => document.getElementById("session-list");

  function readSavedActive() {
    return localStorage.getItem(KEY) || localStorage.getItem(LEGACY_KEY) || "";
  }

  // ---------- 数据 ----------

  async function loadList() {
    const data = await api.sessions();
    list = data.sessions || [];
    render();
  }

  async function init() {
    document.getElementById("new-chat").addEventListener("click", newSession);
    box().addEventListener("click", onListClick);
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        newSession();
      }
    });

    await loadList();

    if (!list.length) {
      const created = await api.createSession();
      await loadList();
      await activate(created.session.session_id);
      return;
    }

    const saved = readSavedActive();
    const pick = list.find((s) => s.session_id === saved) || list[0];
    await activate(pick.session_id);
  }

  async function refresh() {
    await loadList();
    const stillThere = list.find((s) => s.session_id === activeId);
    if (stillThere) {
      chat.updateMeta(stillThere);
      return;
    }
    if (list.length) await activate(list[0].session_id);
  }

  // ---------- 操作 ----------

  async function newSession() {
    try {
      const created = await api.createSession();
      await loadList();
      await activate(created.session.session_id);
      document.getElementById("input")?.focus();
    } catch (e) {
      ui.toast("新建会话失败：" + e.message, "err");
    }
  }

  async function activate(id) {
    activeId = id;
    localStorage.setItem(KEY, id);
    localStorage.removeItem(LEGACY_KEY);
    render();
    try {
      await chat.openSession(id);
    } catch (e) {
      ui.toast("打开会话失败：" + e.message, "err");
    }
  }

  async function deleteSession(id) {
    const target = list.find((s) => s.session_id === id);
    if (!confirm(`删除会话「${target?.title || id}」及其全部消息？`)) return;

    try {
      await api.deleteSession(id);
    } catch (e) {
      ui.toast("删除失败：" + e.message, "err");
      return;
    }

    const wasActive = id === activeId;
    if (wasActive) activeId = null;
    await loadList();

    if (!list.length) {
      const created = await api.createSession();
      await loadList();
      await activate(created.session.session_id);
    } else if (wasActive) {
      await activate(list[0].session_id);
    } else {
      render();
    }
    ui.toast("会话已删除", "ok");
  }

  function beginRename(item, id) {
    const target = list.find((s) => s.session_id === id);
    if (!target) return;

    const main = item.querySelector(".session-main");
    const input = ui.el("input", "session-rename-input");
    input.value = target.title || "";
    main.innerHTML = "";
    main.appendChild(input);
    input.focus();
    input.select();

    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      if (save && value && value !== target.title) {
        try {
          await api.renameSession(id, value);
        } catch (e) {
          ui.toast("重命名失败：" + e.message, "err");
        }
      }
      await loadList();
      render();
      if (activeId === id) {
        const updated = list.find((s) => s.session_id === id);
        if (updated) chat.updateMeta(updated);
      }
    };

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  // ---------- 渲染 ----------

  function render() {
    const container = box();
    const count = document.getElementById("session-count");
    if (count) count.textContent = list.length;

    container.innerHTML = "";
    if (!list.length) {
      container.appendChild(ui.el("div", "session-list-empty", "还没有会话\n点「新对话」开始一个"));
      return;
    }

    for (const s of list) {
      const item = ui.el("div", "session-item" + (s.session_id === activeId ? " active" : ""));
      item.dataset.id = s.session_id;
      item.title = s.title || "新对话";

      const main = ui.el("div", "session-main");
      main.appendChild(ui.el("span", "session-title", s.title || "新对话"));
      const turns = s.turn_count ?? s.message_count;
      const meta = [
        `${turns} 轮`,
        ui.relativeTime(s.updated_at),
      ].filter(Boolean).join(" · ");
      main.appendChild(ui.el("span", "session-meta", meta));

      const actions = ui.el("div", "session-actions");
      const edit = ui.el("button", "session-act");
      edit.dataset.act = "rename";
      edit.title = "重命名";
      edit.innerHTML = ui.icon("edit");

      const del = ui.el("button", "session-act");
      del.dataset.act = "delete";
      del.title = "删除";
      del.innerHTML = ui.icon("trash");

      actions.append(edit, del);
      item.append(main, actions);
      container.appendChild(item);
    }

    // 让当前会话保持在可视区
    const activeItem = container.querySelector(".session-item.active");
    activeItem?.scrollIntoView({ block: "nearest" });
  }

  function onListClick(e) {
    const item = e.target.closest(".session-item");
    if (!item) return;
    const id = item.dataset.id;

    const action = e.target.closest("[data-act]");
    if (action) {
      e.stopPropagation();
      if (action.dataset.act === "rename") beginRename(item, id);
      else if (action.dataset.act === "delete") deleteSession(id);
      return;
    }

    if (id !== activeId) activate(id);
  }

  return {
    init,
    refresh,
    newSession,
    find: (id) => list.find((s) => s.session_id === id) || null,
    get activeId() { return activeId; },
  };
})();
