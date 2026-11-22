const csrf = document.querySelector('meta[name="app-token"]').content;
const el = (id) => document.getElementById(id);
const esc = (value) => {
  const div = document.createElement("div");
  div.textContent = String(value == null ? "" : value);
  return div.innerHTML;
};
const money = (value) => Number(value || 0).toLocaleString("ru-RU", {
  minimumFractionDigits: 0, maximumFractionDigits: 2
});

let snapshot = null;
let offersData = null;
let accountData = null;
let accountError = false;
let selectedProfile = localStorage.getItem("selectedProfile") || "";
let selectedOffer = "";
let loading = false;
let currentPage = "overview";

function showPage(page) {
  if (!["overview", "gpu", "storage", "account"].includes(page)) return;
  currentPage = page;
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== page;
  });
  document.querySelectorAll("[data-page]").forEach((item) => {
    const active = item.dataset.page === page;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

document.querySelectorAll("[data-page]").forEach((item) => {
  item.onclick = () => showPage(item.dataset.page);
});

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-App-Token": csrf },
    body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Ошибка запроса");
  return data;
}

function notice(message, kind = "error") {
  el("notice").textContent = message || "";
  el("notice").className = "notice" + (kind === "error" ? " error" : "");
  el("notice").hidden = !message;
}

function renderBalance() {
  const profile = snapshot && snapshot.profiles.find((item) => item.id === selectedProfile);
  el("balance-caption").textContent = accountError ?
    "Баланс временно недоступен" : profile ? profile.label : "Добавь аккаунт в левом меню";
  el("balance-value").innerHTML = accountData && selectedProfile ?
    esc(money(accountData.balance_rub)) + ' <span>₽</span>' : '— <span>₽</span>';
}

function renderProfiles() {
  const profiles = snapshot.profiles;
  if (!profiles.some((profile) => profile.id === selectedProfile)) {
    selectedProfile = profiles[0] ? profiles[0].id : "";
    localStorage.setItem("selectedProfile", selectedProfile);
  }
  el("profiles").innerHTML = '<option value="">Выбери аккаунт</option>' +
    profiles.map((profile) => '<option value="' + esc(profile.id) + '">' +
      esc(profile.label) + (profile.email ? " · " + esc(profile.email) : "") +
      "</option>").join("");
  el("profiles").value = selectedProfile;
  const profile = profiles.find((item) => item.id === selectedProfile);
  el("delete").disabled = !profile;
  if (!profile) {
    el("profile-details").textContent = "Добавь или выбери аккаунт.";
    return;
  }
  el("profile-details").innerHTML =
    "<p><strong>Лимиты:</strong> " + money(profile.max_rate) + " ₽/час · " +
    esc(profile.max_minutes) + " мин · " + money(profile.monthly_limit) + " ₽/месяц</p>" +
    '<p>В Тераферме открой «Настройки → Доступ к серверам» и добавь этот публичный ключ отдельной строкой. У разных профилей могут быть разные ключи.</p>' +
    '<div class="key-box"><code>' + esc(profile.public_key) + '</code>' +
    '<button id="copy-key" class="button secondary" type="button">Копировать</button></div>';
  el("copy-key").onclick = async () => {
    await navigator.clipboard.writeText(profile.public_key);
    el("copy-key").textContent = "Скопировано";
  };
}

function renderOffers() {
  const accountInfo = el("account-info");
  if (!selectedProfile) {
    accountInfo.textContent = "Добавь аккаунт Teraferma.";
    el("overview-offer").textContent = "Аккаунт ещё не выбран";
    el("offers").textContent = "Машины пока не загружены.";
    el("price-note").hidden = true;
    el("start").disabled = true;
    return;
  }
  if (!offersData) {
    accountInfo.textContent = "Загружаю каталог…";
    el("overview-offer").textContent = "Ищу доступную GPU…";
    el("offers").textContent = "Загружаю…";
    el("price-note").hidden = true;
    el("start").disabled = true;
    return;
  }
  const account = offersData.account;
  if (!accountData) {
    accountData = account;
    renderBalance();
  }
  accountInfo.textContent = "Почта: " + (account.verified ? "подтверждена" : "не подтверждена") +
    " · SSH: " + (account.ssh_key_set ? "ключ добавлен в кабинете" : "нужен ключ");
  if (!offersData.live) {
    el("offers").textContent = "Каталог Teraferma сейчас недоступен.";
    el("overview-offer").textContent = "Каталог GPU недоступен";
    el("start").disabled = true;
    return;
  }
  const offers = offersData.offers;
  if (!offers.length) {
    el("offers").textContent = "Подходящих GPU в установленном лимите сейчас нет.";
    el("overview-offer").textContent = "Нет доступной GPU · проверь раздел GPU";
    el("start").disabled = true;
    return;
  }
  if (!offers.some((item) => String(item.id) === selectedOffer)) {
    selectedOffer = String(offers[0].id);
  }
  el("offers").innerHTML = offers.map((offer) =>
    '<label class="offer ' + (String(offer.id) === selectedOffer ? "selected" : "") + '">' +
    '<input type="radio" name="offer" value="' + esc(offer.id) + '" ' +
    (String(offer.id) === selectedOffer ? "checked" : "") + ">" +
    '<span class="offer-main"><strong>' + esc(offer.gpu_name) + " · " +
    esc(offer.vram) + ' ГБ</strong><small>' + esc(offer.country) +
    " · ОЗУ " + esc(offer.ram) + " ГБ · трафик " +
    money(offer.traffic_rub) + " ₽/ГБ</small></span>" +
    '<span class="offer-price">' + money(offer.price) + " ₽/ч</span></label>"
  ).join("");
  el("offers").querySelectorAll('input[name="offer"]').forEach((radio) => {
    radio.onchange = () => { selectedOffer = radio.value; renderOffers(); };
  });
  const offer = offers.find((item) => String(item.id) === selectedOffer);
  if (offer) {
    el("overview-offer").textContent = offer.gpu_name + " · " +
      money(offer.price) + " ₽/час · выбрать другую в разделе GPU";
    el("price-note").textContent = "Выбрано: " + money(offer.price) +
      " ₽/час. Минимум за 15 минут: " + money(offer.price / 4) +
      " ₽ + трафик. На балансе нужна сумма минимум на час.";
    el("price-note").hidden = false;
  }
  const rental = snapshot.rental;
  const active = !rental.confirmed_stopped && rental.status !== "idle";
  el("start").disabled = active || !offer || !account.verified || !account.ssh_key_set;
}

function renderRental() {
  const rental = snapshot.rental;
  const labels = {
    idle: "Сервер не запущен", starting: "Проверка", creating: "Заказ сервера",
    provisioning: "Подготовка GPU", loading: "Загрузка модели",
    ready: "Qwen готова", stopping: "Остановка", recovering: "Восстановление",
    stopped: "Сервер остановлен", error: "Ошибка запуска",
    creation_uncertain: "Требуется проверка", stop_failed: "Остановка не подтверждена",
    needs_attention: "Нужно восстановить соединение"
  };
  el("status-title").textContent = labels[rental.status] || rental.status;
  const stageNames = {
    RESTORING: "ВОССТАНОВЛЕНИЕ МОДЕЛИ", DOWNLOADING: "ПЕРВАЯ ЗАГРУЗКА МОДЕЛИ",
    BUILDING: "ПОДГОТОВКА ДВИЖКА", LOADING: "ЗАГРУЗКА В ВИДЕОПАМЯТЬ"
  };
  const stages = {
    starting: [5, "ПРОВЕРКА АККАУНТА"], creating: [16, "ЗАКАЗ GPU"],
    provisioning: [28, "ПОДГОТОВКА СЕРВЕРА"], loading: [38, "ПОДГОТОВКА МОДЕЛИ"],
    ready: [100, "ГОТОВО"], recovering: [95, "ВОССТАНОВЛЕНИЕ СВЯЗИ"]
  };
  const progress = stages[rental.status];
  el("launch-progress").hidden = !progress;
  if (progress) {
    let [percent, stage] = progress;
    const remoteStage = String(rental.progress_stage || "").trim();
    if (rental.status === "loading") {
      stage = stageNames[remoteStage] || stage;
      if (["RESTORING", "DOWNLOADING"].includes(remoteStage)) {
        const fraction = Math.min(1, Math.max(0,
          Number(rental.progress_bytes || 0) / Number(rental.progress_total || 24945124640)));
        percent = Math.round(38 + fraction * 43);
      } else if (remoteStage === "BUILDING") {
        percent = Math.round(82 + Math.min(100, Number(rental.progress_build || 0)) * 0.1);
      }
      else if (remoteStage === "LOADING") percent = 93;
    }
    el("progress-stage").textContent = stage;
    el("progress-percent").textContent = percent + "%";
    el("progress-fill").style.width = percent + "%";
    el("progress-track").setAttribute("aria-valuenow", String(percent));
    el("progress-detail").textContent = ["RESTORING", "DOWNLOADING"].includes(remoteStage) ?
      (Number(rental.progress_bytes || 0) / 1e9).toFixed(1) + " / " +
      (Number(rental.progress_total || 24945124640) / 1e9).toFixed(1) + " ГБ" :
      (remoteStage === "BUILDING" ? "СБОРКА " + Number(rental.progress_build || 0) + "%" :
        rental.status === "ready" ? "МОДЕЛЬ ОТВЕЧАЕТ" : "ПОСЛЕДОВАТЕЛЬНЫЕ ЭТАПЫ ЗАПУСКА");
  }
  el("status-message").textContent = rental.message || "";
  if (rental.error && rental.error !== rental.message && rental.error !== "Остановлено пользователем") {
    el("status-message").textContent += "\n" + rental.error;
  }
  el("status-dot").className = "dot" +
    (rental.status === "ready" ? " active" :
      ["error", "stop_failed", "creation_uncertain", "needs_attention"].includes(rental.status) ? " bad" : "");
  const meta = [];
  if (rental.rate) meta.push("Тариф: " + money(rental.rate) + " ₽/час");
  if (rental.instance_id) meta.push("Сервер №" + rental.instance_id);
  if (rental.instance_id) meta.push("Списано: " + money(rental.cost) + " ₽");
  if (rental.stop_at && !rental.confirmed_stopped) {
    meta.push("Автостоп: " + new Date(rental.stop_at * 1000).toLocaleTimeString("ru-RU"));
  } else if (rental.keep_running && !rental.confirmed_stopped) {
    meta.push("Работает до ручной остановки");
  }
  el("status-meta").textContent = rental.confirmed_stopped ? "" : meta.join(" · ");
  el("stop").hidden = rental.confirmed_stopped || rental.status === "idle";
  el("stop").disabled = rental.confirmed_stopped || rental.status === "idle";
  el("start").hidden = !rental.confirmed_stopped && rental.status !== "idle";
  el("keep-running").hidden = rental.confirmed_stopped || !rental.instance_id || !rental.stop_at;
  el("reconnect").hidden = rental.status !== "needs_attention";
  el("opencode").hidden = !["ready", "recovering"].includes(rental.status);
  el("opencode").href = snapshot.opencode_url || snapshot.model_url;
  el("opencode").textContent = snapshot.opencode_url ? "↗ ОТКРЫТЬ OPENCODE" : "↗ ОТКРЫТЬ ЧАТ QWEN";
}

function renderStorage() {
  const storage = (snapshot.storage || {})[selectedProfile];
  const button = el("save-on-server");
  button.disabled = !selectedProfile || !!storage && [
    "starting", "creating", "provisioning", "downloading", "downloaded",
    "saving", "saved"
  ].includes(storage.status);
  button.textContent = storage && storage.status === "saved" ?
    "Qwen сохранена на Teraferma" : "Сохранить Qwen на Teraferma";
  if (!selectedProfile) {
    el("storage-status").textContent = "Выбери аккаунт.";
    el("storage-meta").textContent = "";
    return;
  }
  el("storage-status").textContent = storage ?
    (storage.message || "Состояние сохранения: " + storage.status) +
    (storage.error ? "\n" + storage.error : "") :
    "На этом аккаунте сохранённой копии пока нет.";
  const meta = [];
  if (storage && storage.instance_id) meta.push("Временный сервер №" + storage.instance_id);
  if (storage && storage.bytes) meta.push("Загружено: " +
    (Number(storage.bytes) / 1e9).toFixed(1) + " ГБ");
  if (storage && storage.cost) meta.push("Перенос: " + money(storage.cost) + " ₽");
  if (storage && storage.status === "saved") {
    meta.push("Хранение: около " + money(storage.monthly_storage_estimate) + " ₽/месяц");
  }
  el("storage-meta").textContent = meta.join(" · ");
}

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    snapshot = await api("/api/state");
    renderProfiles();
    renderBalance();
    renderRental();
    renderStorage();
    renderOffers();
    if (!snapshot.profiles.length && currentPage === "overview") showPage("account");
  } catch (error) {
    notice(error.message);
  } finally {
    loading = false;
  }
}

async function refreshOffers() {
  if (!selectedProfile) return;
  const profileId = selectedProfile;
  offersData = null;
  renderOffers();
  try {
    const result = await api("/api/offers?profile_id=" + encodeURIComponent(profileId));
    if (profileId !== selectedProfile) return;
    offersData = result;
    notice("");
  } catch (error) {
    if (profileId === selectedProfile) notice(error.message);
  }
  renderOffers();
}

async function refreshAccount() {
  if (!selectedProfile) return;
  const profileId = selectedProfile;
  try {
    const account = await api("/api/account?profile_id=" + encodeURIComponent(profileId));
    if (profileId !== selectedProfile) return;
    accountData = account;
    accountError = false;
    renderBalance();
  } catch (error) {
    if (profileId === selectedProfile) {
      accountError = true;
      renderBalance();
    }
  }
}

el("profiles").onchange = () => {
  selectedProfile = el("profiles").value;
  localStorage.setItem("selectedProfile", selectedProfile);
  selectedOffer = "";
  offersData = null;
  accountData = null;
  accountError = false;
  renderProfiles();
  renderBalance();
  renderStorage();
  renderOffers();
  refreshOffers();
  refreshAccount();
};
el("refresh-offers").onclick = refreshOffers;
el("refresh-balance").onclick = refreshAccount;
el("add-profile").onsubmit = async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api("/api/profiles", values);
    selectedProfile = result.profile.id;
    localStorage.setItem("selectedProfile", selectedProfile);
    el("token").value = "";
    el("add-box").open = false;
    await refresh();
    await refreshOffers();
    await refreshAccount();
    showPage("account");
    notice(result.account.ssh_key_set ?
      "Аккаунт сохранён. Проверь, что в кабинете указан показанный SSH-ключ." :
      "Аккаунт сохранён. Скопируй публичный SSH-ключ в кабинет Teraferma.", "success");
  } catch (error) { notice(error.message); }
};
el("import").onclick = async () => {
  try {
    const result = await api("/api/import-existing", {});
    selectedProfile = result.profile.id;
    localStorage.setItem("selectedProfile", selectedProfile);
    await refresh();
    await refreshOffers();
    await refreshAccount();
    showPage("overview");
    notice("Прежний аккаунт импортирован в этот проект.", "success");
  } catch (error) { notice(error.message); }
};
el("delete").onclick = async () => {
  if (!selectedProfile || !confirm("Удалить профиль и его локальные ключи?")) return;
  try {
    await api("/api/profiles/delete", { id: selectedProfile });
    selectedProfile = "";
    offersData = null;
    accountData = null;
    accountError = false;
    await refresh();
    await refreshOffers();
    await refreshAccount();
    notice("Профиль удалён.", "success");
  } catch (error) { notice(error.message); }
};
el("start").onclick = async () => {
  if (!selectedProfile || !selectedOffer) return;
  try {
    const profileId = selectedProfile;
    const offer = offersData?.offers.find(row => String(row.id) === selectedOffer);
    if (!offer) return;
    await refreshOffers();
    if (profileId !== selectedProfile || !offersData?.live) return;
    const current = offersData.offers.find(row => String(row.id) === String(offer.id));
    if (!current || selectedOffer !== String(offer.id) || current.price !== offer.price ||
        current.traffic_rub !== offer.traffic_rub) {
      notice("Каталог GPU изменился. Проверь доступную машину и цену перед запуском.");
      return;
    }
    await api("/api/start", { profile_id: profileId, offer_id: current.id });
    notice("");
    await refresh();
    showPage("overview");
  } catch (error) { notice(error.message); }
};
el("stop").onclick = async () => {
  try {
    await api("/api/stop", {});
    notice("");
    await refresh();
  } catch (error) { notice(error.message); }
};
el("keep-running").onclick = async () => {
  try {
    await api("/api/keep-running", {});
    notice("Автостоп отключён. Останови аренду вручную после работы.", "success");
    await refresh();
  } catch (error) { notice(error.message); }
};
el("reconnect").onclick = async () => {
  try {
    await api("/api/reconnect", {});
    notice("");
    await refresh();
  } catch (error) { notice(error.message); }
};
el("save-on-server").onclick = async () => {
  if (!selectedProfile) return;
  try {
    await api("/api/save-on-server", { profile_id: selectedProfile });
    notice("Перенос начался. Прогресс появится ниже.", "success");
    await refresh();
  } catch (error) { notice(error.message); }
};

function renderDemo() {
  const params = new URLSearchParams(location.search);
  const loadingDemo = params.get("stage") === "loading";
  selectedProfile = "demo";
  selectedOffer = "1";
  accountData = { balance_rub: 1240.50, verified: true, ssh_key_set: true };
  offersData = {
    account: accountData, live: true,
    offers: [{ id: 1, gpu_name: "RTX A6000", vram: 48, ram: 64,
      country: "EU", traffic_rub: 0, price: 69 }]
  };
  snapshot = {
    profiles: [{ id: "demo", label: "Демо аккаунт", email: "",
      max_rate: 80, max_minutes: 120, monthly_limit: 3000,
      public_key: "ssh-ed25519 DEMO" }],
    rental: loadingDemo ? {
      status: "loading", confirmed_stopped: false, instance_id: 123,
      rate: 69, cost: 18.4, progress_stage: "RESTORING",
      progress_bytes: 12500000000, progress_total: 24945124640,
      message: "Восстанавливаю модель из хранилища Teraferma"
    } : {
      status: "stopped", confirmed_stopped: true,
      message: "Сервер остановлен"
    },
    storage: { demo: {
      status: "saved", message: "Qwen и движок сохранены на сервере",
      bytes: 24945124640, monthly_storage_estimate: 75.13
    } },
    opencode_url: "#"
  };
  renderProfiles();
  renderBalance();
  renderRental();
  renderStorage();
  renderOffers();
  document.querySelectorAll("button:not(.nav-item)").forEach((button) => { button.disabled = true; });
  el("opencode").hidden = true;
  document.querySelector(".topbar-right").textContent = "ДЕМО";
  showPage(params.get("view") || "overview");
}

if (new URLSearchParams(location.search).has("demo")) {
  renderDemo();
} else {
  refresh().then(() => Promise.all([refreshOffers(), refreshAccount()]));
  setInterval(refresh, 3000);
  setInterval(refreshOffers, 60000);
  setInterval(refreshAccount, 30000);
}
