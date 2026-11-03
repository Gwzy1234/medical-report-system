-- ============================================================================
--  医学影像报告模板管理系统 —— SQLite 3 数据库初始化脚本
--  文件位置：sql/init_db.sql
--  特  点：可重复执行（CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE）
--  说  明：程序启动时由 app/database.py 的 init_db() 自动执行本脚本；
--          也可以手动执行：
--          python -c "import sqlite3,pathlib; c=sqlite3.connect('medical_report.db'); c.executescript(pathlib.Path('sql/init_db.sql').read_text(encoding='utf-8')); c.commit(); c.close()"
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 如需“清空数据库后重建”，取消下面 4 行的注释（会删除全部数据！）
-- DROP TABLE IF EXISTS reports;
-- DROP TABLE IF EXISTS templates;
-- DROP TABLE IF EXISTS prompt_configs;
-- DROP TABLE IF EXISTS users;
-- ----------------------------------------------------------------------------


-- ============================================================================
-- 1. users —— 系统用户表（医生 / 管理员共用）
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE,              -- 登录名，唯一且不区分大小写
    password_hash TEXT    NOT NULL,                             -- hashlib.sha256 十六进制摘要
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CONSTRAINT uq_users_username UNIQUE (username)
);

-- ============================================================================
-- 2. prompt_configs —— 各检查分类的大模型提示词配置
-- ============================================================================
CREATE TABLE IF NOT EXISTS prompt_configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL UNIQUE,                      -- 检查分类，如：头颅CT、胸部CT
    system_prompt TEXT    NOT NULL,                             -- 该分类对应的 System Prompt
    is_active     INTEGER NOT NULL DEFAULT 1
                          CHECK (is_active IN (0, 1))           -- 1=启用，0=停用
);

-- ============================================================================
-- 3. templates —— 报告模板表
-- ============================================================================
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,                           -- 模板标题
    category        TEXT    NOT NULL,                           -- 检查分类
    findings        TEXT    NOT NULL DEFAULT '',                -- 影像所见
    conclusion      TEXT    NOT NULL DEFAULT '',                -- 诊断意见
    is_ai_generated INTEGER NOT NULL DEFAULT 0
                            CHECK (is_ai_generated IN (0, 1)),  -- 1=AI 参与生成
    creator_id      INTEGER,                                    -- 创建人
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (creator_id) REFERENCES users (id) ON DELETE SET NULL
);

-- ============================================================================
-- 4. reports —— 患者影像报告表
-- ============================================================================
CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id       INTEGER,                                  -- 来源模板，可为空
    patient_name      TEXT    NOT NULL,                         -- 患者姓名
    patient_gender    TEXT    NOT NULL DEFAULT '未知',          -- 性别：男 / 女 / 未知
    patient_age       INTEGER
                      CHECK (patient_age IS NULL OR (patient_age >= 0 AND patient_age <= 150)),
    patient_record_no TEXT    NOT NULL DEFAULT '',              -- 病历号 / 影像号
    findings          TEXT    NOT NULL,                         -- 影像所见（快照）
    conclusion        TEXT    NOT NULL,                         -- 诊断意见（快照）
    doctor_id         INTEGER,                                  -- 报告医生
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (template_id) REFERENCES templates (id) ON DELETE SET NULL,
    FOREIGN KEY (doctor_id)   REFERENCES users (id)     ON DELETE SET NULL
);

-- ============================================================================
-- 常用索引（加速分类筛选、按医生/时间检索）
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_templates_category   ON templates (category);
CREATE INDEX IF NOT EXISTS idx_templates_creator    ON templates (creator_id);
CREATE INDEX IF NOT EXISTS idx_templates_created_at ON templates (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reports_doctor       ON reports (doctor_id);
CREATE INDEX IF NOT EXISTS idx_reports_template     ON reports (template_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_at   ON reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_patient_name ON reports (patient_name);

CREATE INDEX IF NOT EXISTS idx_prompt_active        ON prompt_configs (category, is_active);

-- ============================================================================
-- 初始化数据：各检查分类的 System Prompt
-- 说明：INSERT OR IGNORE 保证重复执行不会覆盖已修改过的提示词；
--       若需更新，请直接 UPDATE prompt_configs SET system_prompt = '...' WHERE category = '...';
-- ============================================================================
INSERT OR IGNORE INTO prompt_configs (category, system_prompt, is_active) VALUES
('头颅CT',
 '你是一名三甲医院影像科主治医师，擅长头颅CT诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按病变主次分条书写，每条以“1.”“2.”编号；3. 先写主要诊断，再写次要发现，最后给出复查或进一步检查建议；4. 措辞严谨，不确定时使用“考虑”“可能”“待排”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('胸部CT',
 '你是一名三甲医院影像科主治医师，擅长胸部CT诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按“主要病变—次要病变—建议”的顺序分条书写，每条以“1.”“2.”编号；3. 对肺结节需写明位置、大小、密度及随访建议；4. 措辞严谨，不确定时使用“考虑”“可能”“待排”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('腹部CT',
 '你是一名三甲医院影像科主治医师，擅长腹部CT诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按“主要病变—次要病变—建议”的顺序分条书写，每条以“1.”“2.”编号；3. 涉及肝胆胰脾肾及腹腔、腹膜后时，分别给出结论；4. 措辞严谨，不确定时使用“考虑”“可能”“待排”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('胸部X线',
 '你是一名三甲医院影像科主治医师，擅长胸部X线（DR）诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按病变主次分条书写，每条以“1.”“2.”编号；3. 对肺部感染、胸腔积液、心影增大等常见征象给出明确结论；4. 措辞严谨，不确定时使用“考虑”“可能”“建议CT进一步检查”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('腹部超声',
 '你是一名三甲医院超声科主治医师，擅长腹部超声诊断。请根据用户提供的“影像所见”，撰写规范的“超声诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按“主要病变—次要病变—建议”的顺序分条书写，每条以“1.”“2.”编号；3. 对脂肪肝、胆囊结石、肾结石、囊肿等常见病变给出程度分级；4. 措辞严谨，不确定时使用“考虑”“可能”“建议进一步检查”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('头颅MRI',
 '你是一名三甲医院影像科主治医师，擅长头颅MRI诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按病变主次分条书写，每条以“1.”“2.”编号；3. 对梗死、出血、占位、脱髓鞘等病变写明部位与范围，必要时建议增强扫描；4. 措辞严谨，不确定时使用“考虑”“可能”“待排”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('脊柱MRI',
 '你是一名三甲医院影像科主治医师，擅长脊柱MRI诊断。请根据用户提供的“影像所见”，撰写规范的“诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按“椎间盘—椎管—脊髓—其他”的顺序分条书写，每条以“1.”“2.”编号；3. 写明椎间盘突出的节段、方向及程度，是否压迫硬膜囊或神经根；4. 措辞严谨，不确定时使用“考虑”“可能”等表述；5. 不得编造影像所见中未提及的信息。',
 1),
('乳腺超声',
 '你是一名三甲医院超声科主治医师，擅长乳腺超声诊断。请根据用户提供的“影像所见”，撰写规范的“超声诊断意见”。要求：1. 只输出诊断意见正文，不要复述影像所见，不要使用 Markdown 标记；2. 按病变主次分条书写，每条以“1.”“2.”编号；3. 对结节写明位置、大小、形态、边界、血流及 BI-RADS 分类；4. 措辞严谨，不确定时使用“考虑”“可能”“建议穿刺活检”等表述；5. 不得编造影像所见中未提及的信息。',
 1);

-- ============================================================================
-- 演示账号（可选）：admin / 123456
-- 密码为 hashlib.sha256('123456') 的结果；正式使用/答辩前请删除或修改。
-- ============================================================================
INSERT OR IGNORE INTO users (username, password_hash)
VALUES ('admin', '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92');

-- ============================================================================
-- 预置报告模板（演示用，方便开箱即用）
-- 说明：creator_id 设为 1（即上面的 admin）；templates 表无唯一约束，
--       因此这里不能像 prompt_configs 那样用 INSERT OR IGNORE 去重。
--       改用「先查再插」：仅当同标题模板不存在时才插入，保证脚本可重复执行。
--       正式使用/答辩前如需清理，可 DELETE FROM templates;
-- ============================================================================
INSERT INTO templates (title, category, findings, conclusion, is_ai_generated, creator_id)
SELECT '头颅CT平扫模板', '头颅CT',
       '双侧大脑半球对称，脑实质未见明显异常密度影，脑室系统无扩大，脑沟脑回清晰，中线结构居中。',
       '头颅CT平扫未见明显异常。',
       0, 1
WHERE NOT EXISTS (SELECT 1 FROM templates WHERE title = '头颅CT平扫模板');

INSERT INTO templates (title, category, findings, conclusion, is_ai_generated, creator_id)
SELECT '胸部CT平扫模板', '胸部CT',
       '胸廓对称，双肺纹理清晰，未见明确结节及实变影；纵隔居中，心影大小、形态未见异常；双侧胸腔未见积液。',
       '胸部CT平扫未见明显异常。',
       0, 1
WHERE NOT EXISTS (SELECT 1 FROM templates WHERE title = '胸部CT平扫模板');

INSERT INTO templates (title, category, findings, conclusion, is_ai_generated, creator_id)
SELECT '腹部CT平扫模板', '腹部CT',
       '肝脏形态、大小正常，边缘光滑，实质密度均匀；胆囊形态、大小正常，壁不厚，腔内未见异常密度影；胰腺、脾脏及双肾形态、大小正常，实质未见异常。',
       '腹部CT平扫未见明显异常。',
       0, 1
WHERE NOT EXISTS (SELECT 1 FROM templates WHERE title = '腹部CT平扫模板');

-- ============================================================================
-- 自检（可选）：
-- SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name;
-- SELECT id, category, is_active FROM prompt_configs;
-- ============================================================================
