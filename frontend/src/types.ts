export type TaskStatus = 'created' | 'waiting_login' | 'running' | 'verifying' | 'paused' | 'completed' | 'partial' | 'failed' | 'private';
export type VideoListKind = 'likes' | 'recommendations';
export type TaskKind = 'comments' | VideoListKind;
export type ListVisibility = 'unknown' | 'public' | 'private';

export interface Task {
  id: string;
  kind: TaskKind;
  target_account: string;
  target_sec_uid: string;
  target_nickname: string;
  visibility: ListVisibility;
  video_count: number;
  max_videos: number;
  start_time: number | null;
  end_time: number | null;
  list_order: 'activity' | 'publication';
  source_url: string;
  video_id: string | null;
  title: string | null;
  status: TaskStatus;
  created_at: number;
  updated_at: number;
  result_version?: number;
  error: string | null;
  main_count: number;
  reply_count: number;
  unknown_authors: number;
  collection_complete: boolean;
  verification_complete: boolean;
  verify_authors: boolean;
}

export interface CollectedVideo {
  video_id: string;
  title: string | null;
  author_name: string | null;
  author_douyin_id: string | null;
  created_at: number | null;
  time_text: string | null;
  video_url: string;
}

export interface VideoPage {
  result_version?: number;
  items: CollectedVideo[];
  total: number;
  page: number;
  page_size: number;
  collection_complete: boolean;
  visibility: ListVisibility;
  unknown_authors?: number;
  verification_complete?: boolean;
}

export interface VideoListDraft {
  account: string;
  maxVideos: string;
  start: string;
  end: string;
}

export interface Session {
  status: 'logged_out' | 'waiting' | 'ready' | 'error';
  error: string | null;
  control_status?: 'ready' | 'cooldown' | 'protected' | 'paused' | 'verification' | 'error';
  cooldown_until?: number;
  guard_until?: number;
  control_reason?: string;
}

export interface Comment {
  comment_id: string;
  video_id: string;
  parent_id: string | null;
  nickname: string | null;
  douyin_id: string | null;
  text: string;
  created_at: number | null;
  time_text: string | null;
  ip_label: string | null;
  match_type: 'contains' | 'fuzzy' | null;
  score: number | null;
}

export interface CommentPage {
  result_version?: number;
  items: Comment[];
  total: number;
  page: number;
  page_size: number;
  unknown_authors: number;
  missing_times: number;
  collection_complete: boolean;
  verification_complete: boolean;
}

export interface Filters {
  account: string;
  keyword: string;
  mode: 'contains' | 'fuzzy';
  threshold: number;
  start: string;
  end: string;
}
