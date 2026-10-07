-- ============================================================
--  TeamToDo v3 数据库 Schema
-- ============================================================
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=15000;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA cache_size=-64000;

-- ============================================================
--  组织架构表（树形）
-- ============================================================
CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    parent_id   INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    sort_order  INTEGER DEFAULT 0,
    description TEXT,
    created_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);

INSERT OR IGNORE INTO organizations (id, name) VALUES (1, '总公司');

-- ============================================================
--  岗位表（可自定义岗位名称和权限）
-- ============================================================
CREATE TABLE IF NOT EXISTS roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    is_system   INTEGER DEFAULT 0,
    created_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);

INSERT OR IGNORE INTO roles (id, name, description, is_system) VALUES
(1, '超级管理员', '系统最高权限', 1),
(2, '管理员',     '部门管理权限', 1),
(3, '普通用户',   '基础使用权限', 1);

-- ============================================================
--  岗位权限表
-- ============================================================
CREATE TABLE IF NOT EXISTS role_permissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id      INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    permission   TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(role_id, permission)
);

INSERT OR IGNORE INTO role_permissions (role_id, permission) VALUES
(1, 'task.create'), (1, 'task.edit'), (1, 'task.delete'), (1, 'task.assign'),
(1, 'task.view_all'), (1, 'task.view_org'),
(1, 'user.manage'), (1, 'user.create'), (1, 'user.edit'), (1, 'user.disable'),
(1, 'org.manage'), (1, 'org.create'), (1, 'org.edit'), (1, 'org.delete'),
(1, 'role.manage'), (1, 'role.create'), (1, 'role.edit'), (1, 'role.delete'),
(1, 'report.view'), (1, 'settings.manage'),
(2, 'task.create'), (2, 'task.edit'), (2, 'task.delete'), (2, 'task.assign'),
(2, 'task.view_all'), (2, 'task.view_org'),
(2, 'user.manage'), (2, 'user.create'), (2, 'user.edit'), (2, 'user.disable'),
(2, 'org.manage'), (2, 'org.create'), (2, 'org.edit'),
(2, 'report.view'),
(3, 'task.create'), (3, 'task.edit'), (3, 'task.view_org'),
(3, 'report.view');

-- ============================================================
--  用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    UNIQUE NOT NULL,
    display_name TEXT    NOT NULL,
    password     TEXT    NOT NULL,
    org_id       INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    role         TEXT    DEFAULT 'user',
    role_id      INTEGER REFERENCES roles(id) ON DELETE SET NULL,
    status       TEXT    DEFAULT 'active',
    phone        TEXT,
    email        TEXT,
    avatar_color TEXT    DEFAULT '#2563eb',
    created_at   TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT    DEFAULT (datetime('now', 'localtime'))
);

INSERT OR IGNORE INTO users (id, username, display_name, password, org_id, role, role_id)
VALUES (1, 'admin', '系统管理员', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', 1, 'superadmin', 1);

-- ============================================================
--  任务表
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_no         TEXT,
    title           TEXT    NOT NULL,
    description     TEXT,
    task_type       TEXT    DEFAULT 'NORMAL',
    category        TEXT    DEFAULT '日常任务',
    source          TEXT,
    status          TEXT    DEFAULT 'TODO',
    priority        TEXT    DEFAULT 'M',
    assignee_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    dept_id         INTEGER,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    org_id          INTEGER,
    tags            TEXT,
    due_date        TEXT,
    estimated_hours REAL,
    actual_hours    REAL,
    cycle_rule      TEXT,
    next_trigger    TEXT,
    last_trigger    TEXT,
    parent_id       INTEGER,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    completed_at    TEXT
);

-- ============================================================
--  任务-部门 多对多关联表（一个任务可同时分发给多个部门）
--  注：tasks.dept_id 保留为「主部门」，用于兼容旧查询与向后兼容
-- ============================================================
CREATE TABLE IF NOT EXISTS task_departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    dept_id     INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
    created_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    UNIQUE(task_id, dept_id)
);

-- ============================================================
--  任务-责任人 多对多关联表（一个任务可同时指派给多个责任人）
--  注：tasks.assignee_id 保留为「主责任人」
-- ============================================================
CREATE TABLE IF NOT EXISTS task_assignees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    UNIQUE(task_id, user_id)
);

-- ============================================================
--  任务关联表
-- ============================================================
CREATE TABLE IF NOT EXISTS task_relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    related_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,
    condition       TEXT    DEFAULT 'COMPLETED',
    notify_users    TEXT,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
--  台账收集：模板表
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    columns     TEXT    NOT NULL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    DEFAULT (datetime('now','localtime'))
);

-- ============================================================
--  台账收集：提交记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger_submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER REFERENCES ledger_templates(id) ON DELETE CASCADE,
    submitter   TEXT    NOT NULL,
    data        TEXT    NOT NULL,
    submitted_at TEXT   DEFAULT (datetime('now','localtime'))
);

-- ============================================================
--  待确认反馈通知表（部门负责人确认）
-- ============================================================
CREATE TABLE IF NOT EXISTS pending_confirmations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id    INTEGER REFERENCES task_comments(id) ON DELETE CASCADE,
    dept_id       INTEGER NOT NULL,
    dept_head_uid INTEGER REFERENCES users(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    task_title    TEXT,
    sender_name   TEXT,
    created_at    TEXT    DEFAULT (datetime('now','localtime')),
    UNIQUE(comment_id, dept_head_uid)
);

-- ============================================================
--  任务反馈表（时间戳自动写入）
-- ============================================================
CREATE TABLE IF NOT EXISTS task_comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    content         TEXT    NOT NULL,
    timestamp       TEXT    DEFAULT (datetime('now', 'localtime')),
    attach_urls     TEXT,
    confirmed_dept  INTEGER DEFAULT 0,
    confirmed_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    confirmed_at    TEXT,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
--  任务关注者表
-- ============================================================
CREATE TABLE IF NOT EXISTS task_followers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    notify      TEXT    DEFAULT 'yes',
    created_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    UNIQUE(task_id, user_id)
);

-- ============================================================
--  操作日志表（审计）
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    table_name  TEXT,
    record_id   INTEGER,
    detail      TEXT,
    ip_addr     TEXT,
    created_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
--  索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee  ON tasks(assignee_id);
CREATE INDEX IF NOT EXISTS idx_tasks_org       ON tasks(org_id);
CREATE INDEX IF NOT EXISTS idx_tasks_dept      ON tasks(dept_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created   ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_due       ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_parent    ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task   ON task_comments(task_id);
CREATE INDEX IF NOT EXISTS idx_comments_user   ON task_comments(user_id);
CREATE INDEX IF NOT EXISTS idx_followers_task  ON task_followers(task_id);
CREATE INDEX IF NOT EXISTS idx_followers_user  ON task_followers(user_id);
CREATE INDEX IF NOT EXISTS idx_pending_dept   ON pending_confirmations(dept_head_uid);
CREATE INDEX IF NOT EXISTS idx_pending_task   ON pending_confirmations(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_users_org       ON users(org_id);
CREATE INDEX IF NOT EXISTS idx_td_task         ON task_departments(task_id);
CREATE INDEX IF NOT EXISTS idx_td_dept         ON task_departments(dept_id);
CREATE INDEX IF NOT EXISTS idx_ta_task         ON task_assignees(task_id);
CREATE INDEX IF NOT EXISTS idx_ta_user         ON task_assignees(user_id);

-- ============================================================
--  触发器：任务完成时自动记录时间
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trig_task_completed
AFTER UPDATE OF status ON tasks
WHEN NEW.status = 'DONE' AND OLD.status != 'DONE'
BEGIN
    UPDATE tasks SET completed_at = datetime('now', 'localtime') WHERE id = NEW.id;
END;
