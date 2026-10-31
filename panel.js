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
        h3 ha-icon { --mdc-icon-size:20px; margin-right:6px; vertical-align:middle; }
        .row { display:grid; gap:4px; padding:8px 0; border-top:1px solid var(--divider-color); }
        button.btn { cursor:pointer; padding:8px 14px; border-radius:8px; border:1px solid var(--primary-color);
          background:transparent; color:var(--primary-color); font-size:14px; margin:6px 6px 0 0; }
        button.btn.fill { background:var(--primary-color); color:var(--text-primary-color,#fff); }
        .log { font-size:.85em; padding:6px 0; border-top:1px solid var(--divider-color); }
        .log small { color:var(--secondary-text-color); }
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
    this._quiet = JSON.parse(JSON.stringify(route.quiet || {}));
    this._renderQuiet();
    d.categories.forEach((c) => {
      const title = c.parent ? c.name : c.group ? `${c.name} (група)` : c.name;
      this._renderTarget(c.id, title + (c.service ? " (службова)" : ""), false, c);
    });

    this._renderTest();
    if (d.is_admin) this._renderJournal();

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
    if (cat && cat.icon) {
      const ic = document.createElement("ha-icon");
      ic.setAttribute("icon", cat.icon);
      h.appendChild(ic);
    }
    h.appendChild(
      document.createTextNode(cat && cat.parent ? `${cat.parent_name} › ${title}` : title)
    );
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
        quiet: this._quietOut(),
      });
      this._d.routes[this._user] = res;
      this._setStatus("Збережено ✓");
    } catch (e) {
      this._setStatus("Не збережено: " + (e.message || e), true);
    }
  }


  // ---------------------------------------------------------------- тихі години

  _quietOut() {
    const q = this._quiet || {};
    return {
      off: !!q.off,
      mode: q.mode || "schedule",
      intervals: (q.intervals || []).map((i) => ({
        days: (i.days || []).map((x) => parseInt(x, 10)),
        from: i.from,
        to: i.to,
      })),
      entity: q.entity && q.entity.entity_id ? q.entity : null,
      behavior: q.behavior || {},
    };
  }

  _quietChanged() {
    clearTimeout(this._t);
    this._t = setTimeout(() => this._save(), 400);
  }

  _renderQuiet() {
    const card = document.createElement("ha-card");
    const q = this._quiet;
    q.mode = q.mode || "schedule";
    q.intervals = q.intervals || [];
    q.behavior = { low: "skip", normal: "defer", high: "silent", ...(q.behavior || {}) };
    q.entity = q.entity || { entity_id: "", op: "is", value: "" };
    const queued = (this._d.queue || {})[this._user] || 0;
    const behOpts = [
      { value: "skip", label: "Пропустити" },
      { value: "silent", label: "Без звуку" },
      { value: "defer", label: "Відкласти до кінця тихих годин" },
    ];
    const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"].map((l, i) => ({
      value: String(i),
      label: l,
    }));
    const L = {
      off: "Вимкнути тихі години (не видаляючи налаштування)",
      mode: "Коли діють тихі години",
      entity_id: "Сутність (наприклад, режим сну на телефоні)",
      op: "Умова",
      value: "Значення",
      low: "Низький пріоритет",
      normal: "Звичайний пріоритет",
      high: "Високий пріоритет",
      days: "Дні тижня (порожньо: щодня)",
      from: "З",
      to: "До",
    };

    const mkForm = (schema, data, onChange) => {
      const f = document.createElement("ha-form");
      f.hass = this._hass;
      f.schema = schema;
      f.data = data;
      f.computeLabel = (s) => L[s.name] || s.name;
      f.addEventListener("value-changed", (ev) => {
        onChange(ev.detail.value);
        this._quietChanged();
      });
      this._forms.push(f);
      return f;
    };

    const h = document.createElement("h3");
    h.textContent = "Тихі години";
    card.appendChild(h);
    const note = document.createElement("div");
    note.className = "msg";
    note.textContent =
      "Критичні сповіщення проходять завжди. Для решти: пропустити, без звуку або відкласти й віддати після закінчення (до 3 окремо на категорію, більше одним зведенням)." +
      (queued ? ` Зараз відкладено: ${queued}.` : "");
    card.appendChild(note);

    const draw = () => {
      card.querySelectorAll(".qbody").forEach((n) => n.remove());
      const body = document.createElement("div");
      body.className = "qbody";
      const top = [
        { name: "off", selector: { boolean: {} } },
        {
          name: "mode",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "schedule", label: "За розкладом" },
                { value: "entity", label: "Коли виконується умова для сутності" },
                { value: "either", label: "За розкладом або за умовою" },
              ],
            },
          },
        },
      ];
      body.appendChild(
        mkForm(top, { off: !!q.off, mode: q.mode }, (v) => {
          const prev = q.mode;
          q.off = !!v.off;
          q.mode = v.mode;
          if (prev !== q.mode) draw();
        })
      );

      if (q.mode !== "entity") {
        q.intervals.forEach((it, idx) => {
          const row = document.createElement("div");
          row.className = "row";
          const sch = [
            { name: "days", selector: { select: { mode: "list", multiple: true, options: DAYS } } },
            {
              type: "grid",
              name: "",
              schema: [
                { name: "from", selector: { time: {} } },
                { name: "to", selector: { time: {} } },
              ],
            },
          ];
          row.appendChild(
            mkForm(
              sch,
              { days: (it.days || []).map(String), from: it.from || "22:00", to: it.to || "07:00" },
              (v) => {
                it.days = v.days || [];
                it.from = v.from;
                it.to = v.to;
              }
            )
          );
          const rm = document.createElement("button");
          rm.className = "btn";
          rm.textContent = "Видалити інтервал";
          rm.onclick = () => {
            q.intervals.splice(idx, 1);
            draw();
            this._quietChanged();
          };
          row.appendChild(rm);
          body.appendChild(row);
        });
        const add = document.createElement("button");
        add.className = "btn";
        add.textContent = "+ Додати інтервал";
        add.onclick = () => {
          q.intervals.push({ days: [], from: "22:00", to: "07:00" });
          draw();
          this._quietChanged();
        };
        body.appendChild(add);
      }

      if (q.mode !== "schedule") {
        const sch = [
          { name: "entity_id", selector: { entity: {} } },
          {
            name: "op",
            selector: {
              select: {
                mode: "dropdown",
                options: [
                  { value: "is", label: "стан дорівнює" },
                  { value: "not", label: "стан не дорівнює" },
                  { value: "gt", label: "число більше за" },
                  { value: "lt", label: "число менше за" },
                ],
              },
            },
          },
          { name: "value", selector: { text: {} } },
        ];
        const row = document.createElement("div");
        row.className = "row";
        row.appendChild(
          mkForm(sch, { entity_id: q.entity.entity_id, op: q.entity.op, value: q.entity.value }, (v) => {
            q.entity = { entity_id: v.entity_id || "", op: v.op || "is", value: v.value || "" };
          })
        );
        body.appendChild(row);
      }

      const beh = [
        { name: "low", selector: { select: { mode: "dropdown", options: behOpts } } },
        { name: "normal", selector: { select: { mode: "dropdown", options: behOpts } } },
        { name: "high", selector: { select: { mode: "dropdown", options: behOpts } } },
      ];
      const row = document.createElement("div");
      row.className = "row";
      row.appendChild(
        mkForm(beh, q.behavior, (v) => {
          q.behavior = { ...q.behavior, ...v };
        })
      );
      body.appendChild(row);
      card.appendChild(body);
    };
    draw();
    this._list.appendChild(card);
  }

  // ------------------------------------------------------------------- тест

  _renderTest() {
    const card = document.createElement("ha-card");
    const h = document.createElement("h3");
    h.textContent = "Надіслати тест";
    card.appendChild(h);
    const note = document.createElement("div");
    note.className = "msg";
    note.textContent =
      "Тест іде за твоїми справжніми маршрутами й тихими годинами, тож видно, що прийде насправді.";
    card.appendChild(note);

    const leaf = this._d.categories.filter((c) => !c.group);
    const state = { category: leaf.length ? leaf[0].id : "", priority: "normal" };
    const f = document.createElement("ha-form");
    f.hass = this._hass;
    f.schema = [
      {
        name: "category",
        selector: {
          select: {
            mode: "dropdown",
            options: leaf.map((c) => ({
              value: c.id,
              label: c.parent ? `${c.parent_name} › ${c.name}` : c.name,
            })),
          },
        },
      },
      {
        name: "priority",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "low", label: "Низький" },
              { value: "normal", label: "Звичайний" },
              { value: "high", label: "Високий" },
              { value: "critical", label: "Критичний" },
            ],
          },
        },
      },
    ];
    f.data = state;
    f.computeLabel = (s) => ({ category: "Категорія", priority: "Пріоритет" })[s.name] || s.name;
    f.addEventListener("value-changed", (ev) => Object.assign(state, ev.detail.value));
    this._forms.push(f);
    card.appendChild(f);

    const out = document.createElement("div");
    out.className = "msg";
    const btn = document.createElement("button");
    btn.className = "btn fill";
    btn.textContent = "Надіслати тест";
    btn.onclick = async () => {
      out.textContent = "Надсилаю…";
      try {
        const r = await this._hass.callWS({
          type: "smart_notify/test",
          user: this._user,
          category: state.category,
          priority: state.priority,
        });
        out.textContent = this._describe(r.results);
      } catch (e) {
        out.textContent = "Помилка: " + (e.message || e);
      }
    };
    card.appendChild(btn);
    card.appendChild(out);
    this._list.appendChild(card);
  }

  _describe(results) {
    const ST = {
      sent: "надіслано",
      failed: "не доставлено",
      off: "вимкнено для цієї категорії",
      filtered: "відфільтровано",
      duplicate: "дублікат",
      quiet_skipped: "пропущено через тихі години",
      deferred: "відкладено до кінця тихих годин",
      expired: "втратило актуальність",
    };
    return (results || [])
      .map((r) => {
        const ch = (r.channels || [])
          .map((c) => `${c.channel}: ${ST[c.status] || c.status}${c.error ? " (" + c.error + ")" : ""}`)
          .join("; ");
        return `${ST[r.status] || r.status}${ch ? " — " + ch : ""}`;
      })
      .join("\n");
  }

  // ----------------------------------------------------------------- журнал

  _renderJournal() {
    const card = document.createElement("ha-card");
    const h = document.createElement("h3");
    h.textContent = "Журнал (останні сповіщення)";
    card.appendChild(h);
    const box = document.createElement("div");
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = "Оновити";
    const load = async () => {
      box.textContent = "Завантажую…";
      try {
        const r = await this._hass.callWS({ type: "smart_notify/journal" });
        box.innerHTML = "";
        if (!r.items.length) box.textContent = "Поки порожньо.";
        r.items.slice(0, 50).forEach((it) => {
          const d = document.createElement("div");
          d.className = "log";
          const when = new Date(it.ts * 1000).toLocaleString();
          const head = document.createElement("div");
          head.textContent = `${it.test ? "[тест] " : ""}${it.category}: ${it.title || it.message}`;
          const sm = document.createElement("small");
          sm.style.whiteSpace = "pre-wrap";
          sm.textContent = `${when}, ${it.priority}\n${this._describe(it.results)}`;
          d.appendChild(head);
          d.appendChild(sm);
          box.appendChild(d);
        });
      } catch (e) {
        box.textContent = "Помилка: " + (e.message || e);
      }
    };
    btn.onclick = load;
    card.appendChild(btn);
    card.appendChild(box);
    this._list.appendChild(card);
    load();
  }

  _setStatus(text, err = false) {
    this._status.textContent = text;
    this._status.className = "status" + (err ? " err" : "");
  }
}
customElements.define("smart-notify-panel", SmartNotifyPanel);