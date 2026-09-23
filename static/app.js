const menuButton = document.querySelector("[data-menu-button]");
const menuOverlay = document.querySelector("[data-menu-overlay]");

function closeMenu() {
    document.body.classList.remove("menu-open");
}

if (menuButton) {
    menuButton.addEventListener("click", () => document.body.classList.toggle("menu-open"));
}
if (menuOverlay) {
    menuOverlay.addEventListener("click", closeMenu);
}

document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
});

const currentDate = document.querySelector("[data-current-date]");
if (currentDate) {
    currentDate.textContent = new Intl.DateTimeFormat("ru-RU", {
        day: "numeric",
        month: "long",
        year: "numeric",
    }).format(new Date());
}

const viewButtons = document.querySelectorAll("[data-employee-view]");
const employeeCards = document.querySelector("[data-employee-cards]");
const employeeList = document.querySelector("[data-employee-list]");
if (employeeCards && employeeList) {
    function showEmployeeView(view) {
        employeeCards.hidden = view === "list";
        employeeList.hidden = view !== "list";
        viewButtons.forEach((button) => button.classList.toggle("active", button.dataset.employeeView === view));
    }
    viewButtons.forEach((button) => button.addEventListener("click", () => showEmployeeView(button.dataset.employeeView)));
    showEmployeeView("cards");

    const search = document.querySelector("[data-employee-search]");
    const department = document.querySelector("[data-employee-department]");
    function filterEmployees() {
        const text = search.value.toLocaleLowerCase("ru-RU").trim();
        document.querySelectorAll("[data-employee-item]").forEach((item) => {
            item.hidden = !(item.dataset.name.includes(text) && (!department.value || item.dataset.department === department.value));
        });
    }
    search.addEventListener("input", filterEmployees);
    department.addEventListener("change", filterEmployees);
}

document.querySelectorAll("[data-presence-status]").forEach((select) => {
    const form = select.closest("form");
    const period = form?.querySelector("[data-absence-period]");
    const dates = period ? [...period.querySelectorAll('input[type="date"]')] : [];
    const submit = form?.querySelector('button[type="submit"]');
    function updateAbsenceFields() {
        const option = select.selectedOptions[0];
        const text = option?.textContent.toLocaleLowerCase("ru-RU") || "";
        const absent = option?.dataset.absence === "1" || ["отпуск", "больнич", "командиров", "обучен"].some((word) => text.includes(word));
        if (period) period.hidden = !absent;
        dates.forEach((input) => { input.required = absent; });
        if (submit) submit.disabled = absent && dates.some((input) => !input.value);
    }
    select.addEventListener("change", updateAbsenceFields);
    dates.forEach((input) => input.addEventListener("input", updateAbsenceFields));
    updateAbsenceFields();
});

const priority = document.querySelector("[data-priority]");
const criticalTime = document.querySelector("[data-critical-time]");
if (priority && criticalTime) {
    const input = criticalTime.querySelector("input");
    const updatePriority = () => {
        const critical = priority.value === "Критическая";
        criticalTime.hidden = !critical;
        input.required = critical;
    };
    priority.addEventListener("change", updatePriority);
    updatePriority();
}

const helperContainer = document.querySelector("[data-helper-departments]");
const helperTemplate = document.querySelector("[data-helper-template]");
function bindHelper(row) {
    const department = row.querySelector('select[name="helper_departments"]');
    const people = row.querySelector('select[name="helper_assignees"]');
    const filter = () => [...people.options].forEach((option) => { option.hidden = Boolean(option.dataset.department) && Boolean(department.value) && option.dataset.department !== department.value; });
    department?.addEventListener("change", filter);
    row.querySelector("[data-remove-helper]")?.addEventListener("click", () => row.remove());
    filter();
}
if (helperContainer && helperTemplate) {
    helperContainer.querySelectorAll(".helper-row").forEach(bindHelper);
    document.querySelector("[data-add-helper]")?.addEventListener("click", () => {
        const row = helperTemplate.content.firstElementChild.cloneNode(true);
        helperContainer.append(row);
        bindHelper(row);
    });
}

const positionsContainer = document.querySelector("[data-additional-positions]");
const positionTemplate = document.querySelector("[data-position-template]");
function bindPosition(row) {
    row.querySelector("[data-remove-position]")?.addEventListener("click", () => row.remove());
}
if (positionsContainer && positionTemplate) {
    positionsContainer.querySelectorAll(".position-row").forEach(bindPosition);
    document.querySelector("[data-add-position]")?.addEventListener("click", () => {
        const row = positionTemplate.content.firstElementChild.cloneNode(true);
        positionsContainer.append(row);
        bindPosition(row);
    });
}

const bell = document.querySelector("[data-unread-count]");
if (bell && bell.dataset.sound === "1") {
    const unread = Number(bell.dataset.unreadCount);
    const previous = Number(localStorage.getItem("pioneerUnread") || 0);
    localStorage.setItem("pioneerUnread", String(unread));
    if (unread > previous) {
        const beep = () => {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            const context = new AudioContext();
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            oscillator.frequency.value = 660;
            gain.gain.setValueAtTime(0.07, context.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.18);
            oscillator.connect(gain).connect(context.destination);
            oscillator.start();
            oscillator.stop(context.currentTime + 0.18);
        };
        document.addEventListener("pointerdown", beep, { once: true });
    }
}
