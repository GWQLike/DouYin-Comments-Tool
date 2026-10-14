import type { VideoListKind } from './types';

export const VIDEO_LIST_LABELS: Record<VideoListKind, string> = {
  likes: '喜欢',
  recommendations: '推荐',
};

export const VIDEO_LIST_ACTIONS: Record<VideoListKind, string> = {
  likes: '点赞',
  recommendations: '推荐',
};

export function listSortLabel(kind: VideoListKind, order: 'activity' | 'publication' = 'activity') {
  return order === 'publication' ? '发布时间从新到旧' : `最近${VIDEO_LIST_ACTIONS[kind]}在前`;
}
