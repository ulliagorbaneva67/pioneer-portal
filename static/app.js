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

const workdayTimer = document.querySelector("[data-workday-timer]");
if (workdayTimer) {
    const formatDuration = (seconds) => {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const rest = seconds % 60;
        return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
    };
    if (workdayTimer.dataset.startedAt) {
        const startedAt = new Date(workdayTimer.dataset.startedAt);
        const tick = () => {
            const seconds = Math.max(0, Math.floor((Date.now() - startedAt.getTime()) / 1000));
            workdayTimer.textContent = formatDuration(seconds);
        };
        tick();
        window.setInterval(tick, 1000);
    } else {
        workdayTimer.textContent = formatDuration(Number(workdayTimer.dataset.seconds || 0));
    }
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
    const status = document.querySelector("[data-employee-status]");
    const position = document.querySelector("[data-employee-position]");
    const sort = document.querySelector("[data-employee-sort]");
    function filterEmployees() {
        const text = search.value.toLocaleLowerCase("ru-RU").trim();
        document.querySelectorAll("[data-employee-item]").forEach((item) => {
            item.hidden = !(item.dataset.name.includes(text) && (!department.value || item.dataset.department === department.value) && (!status.value || item.dataset.status === status.value) && (!position.value || item.dataset.position === position.value));
        });
        document.querySelectorAll(".employee-grid, .employee-list tbody").forEach((container) => {
            const items = [...container.querySelectorAll(":scope > [data-employee-item]")];
            items.sort((left, right) => {
                const field = sort.value === "position" ? "position" : sort.value === "status" ? "status" : "fullName";
                const order = left.dataset[field].localeCompare(right.dataset[field], "ru");
                return sort.value === "name-desc" ? -order : order;
            });
            items.forEach((item) => container.append(item));
        });
    }
    search.addEventListener("input", filterEmployees);
    department.addEventListener("change", filterEmployees);
    status.addEventListener("change", filterEmployees);
    position.addEventListener("change", filterEmployees);
    sort.addEventListener("change", filterEmployees);
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
    const initialStatus = select.dataset.currentStatus || select.value;
    select.addEventListener("change", () => {
        if (select.value !== initialStatus) dates.forEach((input) => { input.value = ""; });
        updateAbsenceFields();
    });
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

const birthDate = document.querySelector("[data-birth-date]");
if (birthDate) {
    const agePreview = document.querySelector("[data-age-preview]");
    const updateAge = () => {
        if (!birthDate.value) {
            agePreview.textContent = "Возраст рассчитается автоматически";
            return;
        }
        const born = new Date(`${birthDate.value}T00:00:00`);
        const today = new Date();
        let age = today.getFullYear() - born.getFullYear();
        if (today < new Date(today.getFullYear(), born.getMonth(), born.getDate())) age -= 1;
        agePreview.textContent = age >= 0 ? `Полных лет: ${age}` : "Проверьте дату рождения";
    };
    birthDate.addEventListener("input", updateAge);
    updateAge();
}

const taskAssignees = document.querySelector("[data-task-assignees]");
if (taskAssignees) {
    const updateTaskPositions = () => {
        const selected = new Set([...taskAssignees.selectedOptions].map((option) => option.value));
        document.querySelectorAll("[data-position-for]").forEach((row) => {
            const visible = selected.has(row.dataset.positionFor);
            row.hidden = !visible;
            const select = row.querySelector("select");
            if (select) select.disabled = !visible;
        });
    };
    taskAssignees.addEventListener("change", updateTaskPositions);
    updateTaskPositions();
}

const replacementRole = document.querySelector("[data-replacement-role]");
const rolePerson = document.querySelector("[data-role-person]");
if (replacementRole && rolePerson) {
    const filterReplacementPeople = () => {
        let firstVisible = null;
        [...rolePerson.options].forEach((option) => {
            option.hidden = option.dataset.role !== replacementRole.value;
            option.disabled = option.hidden;
            if (!option.hidden && !firstVisible) firstVisible = option;
        });
        if (firstVisible) rolePerson.value = firstVisible.value;
    };
    replacementRole.addEventListener("change", filterReplacementPeople);
    filterReplacementPeople();
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
if (bell && bell.dataset.employeeId) {
    const storageKey = `pioneerUnread:${bell.dataset.employeeId}`;
    const latestStorageKey = `pioneerLatestNotification:${bell.dataset.employeeId}`;
    let unread = Number(bell.dataset.unreadCount);
    let previous = Number(localStorage.getItem(storageKey) || 0);
    let latest = Number(localStorage.getItem(latestStorageKey) || 0);
    let audioUnlocked = false;
    const beep = () => {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext || bell.dataset.sound !== "1") return;
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
    document.addEventListener("pointerdown", () => { audioUnlocked = true; }, { once: true });
    if (unread > previous && bell.dataset.sound === "1") document.addEventListener("pointerdown", beep, { once: true });
    localStorage.setItem(storageKey, String(unread));
    const refreshNotifications = async () => {
        try {
            const response = await fetch(bell.dataset.statusUrl, { headers: { Accept: "application/json" } });
            if (!response.ok) return;
            const status = await response.json();
            previous = unread;
            unread = Number(status.unread);
            const newLatest = Number(status.latest);
            localStorage.setItem(storageKey, String(unread));
            if (newLatest > latest && latest && audioUnlocked) beep();
            latest = Math.max(latest, newLatest);
            localStorage.setItem(latestStorageKey, String(latest));
            const badge = bell.querySelector(".notification-count");
            if (badge) {
                badge.textContent = unread;
                badge.hidden = unread === 0;
            } else if (unread) {
                const newBadge = document.createElement("span");
                newBadge.className = "notification-count";
                newBadge.textContent = unread;
                bell.append(newBadge);
            }
            const popup = document.querySelector("[data-notice-popup]");
            if (popup && status.popup) {
                const popupKey = `pioneerPopup:${bell.dataset.employeeId}:${status.popup.id}`;
                if (!localStorage.getItem(popupKey)) {
                    popup.querySelector("[data-notice-text]").textContent = status.popup.text;
                    popup.querySelector("[data-notice-link]").href = status.popup.action_url || "/profile";
                    popup.hidden = false;
                    popup.querySelector("[data-notice-close]").onclick = () => {
                        localStorage.setItem(popupKey, "1");
                        popup.hidden = true;
                    };
                }
            }
        } catch (_error) {
            // Портал продолжает работать даже при временной потере связи.
        }
    };
    refreshNotifications();
    window.setInterval(refreshNotifications, 30000);
}
