const GENERAL = "__default";
const PERSISTENT = "persistent";

const empty = () => ({ mode: "inherit", channels: [], fallback: [] });

const modesCat = (c) => [
  {
    value: "inherit",
    label: c.admin_off
      ? "Вимкнено типово (щоб отримувати, обери «Надсилати»)"
      : c.parent
        ? `Як у категорії «${c.parent_name}»`
        : "Як у загальному маршруті",
  },
  { value: "off", label: "Вимкнено" },
  { value: "send", label: "Надсилати" },
];
const MODES_GEN = [
  { value: "inherit", label: "Не налаштовано (лише панель HA)" },
  { value: "off", label: "Вимкнено" },
  { value: "send", label: "Надсилати" },
];

class SmartNotifyPanel extends HTMLElement {
  set hass(h) {
    this._hass = h;
    if (!this._built) {
      this._built = true;
      this._build();
      this._load();
    }
    if (this._userForm) this._userForm.hass = h;
    (this._forms || []).forEach((f) => (f.hass = h));
  }

  set narrow(v) {
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
  }

  _build() {
    this.innerHTML = `
      <style>
        smart-notify-panel { display:block; min-height:100%;
          background:var(--primary-background-color); color:var(--primary-text-color); }
        .bar { display:flex; align-items:center; gap:8px; height:56px; padding:0 8px;
          background:var(--app-header-background-color, var(--primary-color));
          color:var(--app-header-text-color, #fff); font-size:20px; }
        .wrap { max-width:640px; margin:0 auto; padding:16px; display:grid; gap:16px; }
        .msg { color:var(--secondary-text-color); padding:8px 4px; }
        .status { height:20px; font-size:.9em; color:var(--secondary-text-color); text-align:right; }
        .status.err { color:var(--error-color); }
        ha-card { padding:16px; display:block; }
        ha-card h3 { margin:0 0 12px; font-weight:500; }
        #list { display:grid; gap:16px; }
        ha-card.sub { margin-left:16px; border-left:3px solid var(--primary-color); }
      </style>
      <div class="bar">
        <ha-menu-button></ha-menu-button>
        <span>Сповіщення</span>
      </div>
      <div class="wrap">
        <ha-card id="who" hidden></ha-card>
        <div id="list"></div>
        <div class="status" id="status"></div>
      </div>`;
    this._menu = this.querySelector("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    this._who = this.querySelector("#who");
    this._list = this.querySelector("#list");
    this._status = this.querySelector("#status");
  }

  async _load() {
    try {
      this._d = await this._hass.callWS({ type: "smart_notify/get" });
    } catch (e) {
      this._list.innerHTML = `<div class="msg">Не вдалося завантажити: ${e.message || e}</div>`;
      return;
    }
    const ids = Object.keys(this._d.users);
    if (!ids.length) {
      this._list.innerHTML = `<div class="msg">Твій обліковий запис не прив'язаний до person. Попроси адміна прив'язати його в Налаштування → Люди.</div>`;
      return;
    }
    this._user = ids.includes(this._d.me) ? this._d.me : ids[0];
    this._renderUserPicker(ids);
    this._renderForm();
  }

  _renderUserPicker(ids) {
    if (!this._d.is_admin || ids.length < 2) return;
    this._who.hidden = false;
    this._who.innerHTML = "";
    const f = document.createElement("ha-form");
    f.hass = this._hass;
    f.schema = [
      {
        name: "user",
        selector: {
          select: {
            mode: "dropdown",
            options: ids.map((id) => ({ value: id, label: this._d.users[id] })),
          },
        },
      },
    ];
    f.data = { user: this._user };
    f.computeLabel = () => "Налаштовую для користувача";
    f.addEventListener("value-changed", (ev) => {
      const u = ev.detail.value.user;
      if (u && u !== this._user) {
        this._user = u;
        this._renderForm();
      }
    });
    this._who.appendChild(f);
    this._userForm = f;
  }

  _renderForm() {
    const d = this._d;
    const u = this._user;
    this._list.innerHTML = "";
    this._forms = [];
    this._setStatus("");

    if (!d.categories.length) {
      this._list.innerHTML = `<div class="msg">Адмін ще не додав жодної категорії.</div>`;
      return;
    }

    this._chOptions = [
      ...(d.channels[u] || []).map((c) => ({ value: c.id, label: c.name })),
      { value: PERSISTENT, label: "Панель HA (персистентне)" },
    ];

    const route = d.routes[u] || { default: empty(), cats: {} };
    this._cfg = { [GENERAL]: { ...empty(), ...route.default } };
    d.categories.forEach((c) => {
      this._cfg[c.id] = { ...empty(), ...((route.cats || {})[c.id] || {}) };
    });

    this._renderTarget(GENERAL, "Загальний маршрут (усі категорії без свого)", true, null);
    d.categories.forEach((c) => {
      const title = c.parent ? c.name : c.group ? `${c.name} (група)` : c.name;
      this._renderTarget(c.id, title + (c.service ? " (службова)" : ""), false, c);
    });

    if (!(d.channels[u] || []).length) {
      const hint = document.createElement("div");
      hint.className = "msg";
      hint.textContent =
        "Для цього користувача ще немає каналів, доступна лише панель HA. Канали додає адмін у Налаштування → Пристрої та служби → Smart Notify.";
      this._list.appendChild(hint);
    }
  }

  _schema(key, general, cat) {
    const select = (multiple) => ({
      select: { mode: "dropdown", multiple, options: this._chOptions },
    });
    const schema = [
      {
        name: "mode",
        selector: {
          select: { mode: "dropdown", options: general ? MODES_GEN : modesCat(cat) },
        },
      },
    ];
    if (this._cfg[key].mode === "send") {
      schema.push(
        { name: "channels", selector: select(true) },
        { name: "fallback", selector: select(true) }
      );
    }
    return schema;
  }

  _renderTarget(key, title, general, cat) {
    const card = document.createElement("ha-card");
    if (cat && cat.parent) card.classList.add("sub");
    const h = document.createElement("h3");
    h.textContent = cat && cat.parent ? `${cat.parent_name} › ${title}` : title;
    card.appendChild(h);
    if (cat && cat.group) {
      const note = document.createElement("div");
      note.className = "msg";
      note.textContent =
        "Група: ці налаштування діють на всі її підкатегорії, де не задано своє.";
      card.appendChild(note);
    }

    const f = document.createElement("ha-form");
    f.hass = this._hass;
    f.computeLabel = (s) =>
      ({
        mode: "Режим",
        channels: "Надсилати в (в усі вибрані одразу)",
        fallback: "Якщо не доставлено: пробувати по черзі",
      })[s.name] || s.name;
    f.computeHelper = (s) =>
      ({
        fallback:
          "Використовується лише коли жоден з основних каналів не спрацював. Порядок як вибрано.",
      })[s.name] || "";
    const draw = () => {
      f.schema = this._schema(key, general, cat);
      f.data = this._cfg[key];
    };
    draw();

    f.addEventListener("value-changed", (ev) => {
      const prev = this._cfg[key].mode;
      this._cfg[key] = { ...empty(), ...ev.detail.value };
      if (this._cfg[key].mode !== prev) draw();
      else f.data = this._cfg[key];
      clearTimeout(this._t);
      this._t = setTimeout(() => this._save(), 400);
    });

    card.appendChild(f);
    this._list.appendChild(card);
    this._forms.push(f);
  }

  async _save() {
    const { [GENERAL]: def, ...cats } = this._cfg;
    try {
      const res = await this._hass.callWS({
        type: "smart_notify/set",
        user: this._user,
        default: def,
        cats,
      });
      this._d.routes[this._user] = res;
      this._setStatus("Збережено ✓");
    } catch (e) {
      this._setStatus("Не збережено: " + (e.message || e), true);
    }
  }

  _setStatus(text, err = false) {
    this._status.textContent = text;
    this._status.className = "status" + (err ? " err" : "");
  }
}
customElements.define("smart-notify-panel", SmartNotifyPanel);