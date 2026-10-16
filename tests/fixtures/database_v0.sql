-- Synthetic schema and records from the unversioned 00a3047 layout.
-- Kept independent of the production migration so tests can detect omissions.
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, source_url TEXT NOT NULL, video_id TEXT DEFAULT '',
    title TEXT DEFAULT '', status TEXT DEFAULT 'created', created_at REAL,
    updated_at REAL, error TEXT, stop_requested INTEGER DEFAULT 0,
    collection_complete INTEGER DEFAULT 0, verification_complete INTEGER DEFAULT 0,
    verify_authors INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL DEFAULT 'comments',
    target_account TEXT NOT NULL DEFAULT '', target_sec_uid TEXT NOT NULL DEFAULT '',
    target_nickname TEXT NOT NULL DEFAULT '', visibility TEXT NOT NULL DEFAULT 'unknown',
    max_videos INTEGER NOT NULL DEFAULT 0, start_time REAL, end_time REAL,
    list_order TEXT NOT NULL DEFAULT 'publication'
);
CREATE TABLE comments (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    comment_id TEXT NOT NULL, video_id TEXT NOT NULL, parent_id TEXT DEFAULT '',
    text TEXT DEFAULT '', created_at REAL, ip_label TEXT DEFAULT '',
    nickname TEXT DEFAULT '', douyin_id TEXT DEFAULT '', uid TEXT DEFAULT '',
    sec_uid TEXT DEFAULT '', author_key TEXT NOT NULL,
    identity_state TEXT DEFAULT 'unknown', reply_count INTEGER DEFAULT 0,
    PRIMARY KEY (task_id,comment_id)
);
CREATE INDEX comments_author ON comments(task_id,author_key);
CREATE INDEX comments_time ON comments(task_id,created_at DESC,comment_id DESC);
CREATE TABLE authors (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author_key TEXT NOT NULL, uid TEXT DEFAULT '', sec_uid TEXT DEFAULT '',
    douyin_id TEXT DEFAULT '', nickname TEXT DEFAULT '', identity_state TEXT DEFAULT 'unknown',
    PRIMARY KEY(task_id,author_key)
);
CREATE TABLE checkpoints (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL, cursor TEXT DEFAULT '0', done INTEGER DEFAULT 0,
    restarts INTEGER DEFAULT 0, PRIMARY KEY(task_id,thread_id)
);
CREATE TABLE session (id INTEGER PRIMARY KEY CHECK(id=1),status TEXT,error TEXT);
CREATE TABLE liked_videos (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL, title TEXT DEFAULT '', author_name TEXT DEFAULT '',
    author_douyin_id TEXT DEFAULT '', created_at REAL, video_url TEXT NOT NULL,
    author_key TEXT NOT NULL, uid TEXT DEFAULT '', sec_uid TEXT DEFAULT '',
    action_at REAL, collected_order INTEGER, PRIMARY KEY(task_id,video_id)
);
CREATE INDEX likes_time ON liked_videos(task_id,created_at DESC,video_id DESC);
CREATE INDEX likes_author ON liked_videos(task_id,author_key);

INSERT INTO tasks(id,source_url,video_id,title,status,created_at,updated_at,error,
    stop_requested,collection_complete,verification_complete,verify_authors)
VALUES('comments','https://www.douyin.com/video/100','100','评论测试','paused',1700000000,
    1700000100,'模拟中断',1,0,0,0);
INSERT INTO tasks(id,source_url,kind,status,created_at,updated_at,verify_authors,
    target_account,target_sec_uid,target_nickname,visibility,max_videos,list_order)
VALUES('likes','','likes','completed',1700000010,1700000110,1,
    'fixture.target','fixture.sec','测试账号','public',10,'publication');
INSERT INTO tasks(id,source_url,kind,status,created_at,updated_at,verify_authors,
    target_account,target_sec_uid,target_nickname,visibility,max_videos,start_time,end_time,list_order)
VALUES('recommendations','','recommendations','limited',1700000020,1700000120,0,
    'fixture.target','fixture.sec','测试账号','public',2,1600000000,1800000000,'activity');
INSERT INTO comments VALUES('comments','c1','100','','第一条评论',1700000001,'广东',
    '测试作者','fixture.author','fixture.uid','fixture.author.sec','fixture.uid','verified',2);
INSERT INTO comments VALUES('comments','c2','100','c1','謝謝分享',NULL,'','测试访客',
    '','','','comment:c2','unknown',0);
INSERT INTO authors VALUES('comments','fixture.uid','fixture.uid','fixture.author.sec',
    'fixture.author','测试作者','verified');
INSERT INTO authors VALUES('comments','comment:c2','','','','测试访客','unknown');
INSERT INTO checkpoints VALUES('comments','','40',0,1);
INSERT INTO checkpoints VALUES('comments','c1','20',0,0);
INSERT INTO checkpoints VALUES('likes','likes','0',1,0);
INSERT INTO checkpoints VALUES('recommendations','recommendations','1700000200',0,1);
INSERT INTO liked_videos VALUES('likes','v1','示例视频','测试作者','fixture.author',1700000050,
    'https://www.douyin.com/video/101','fixture.uid','fixture.uid','fixture.author.sec',NULL,NULL);
INSERT INTO liked_videos VALUES('recommendations','v2','示例推荐','测试访客','',1700000040,
    'https://www.douyin.com/video/102','fixture.other','fixture.other','fixture.other.sec',1700000200,1);
INSERT INTO session VALUES(1,'logged_in',NULL);
