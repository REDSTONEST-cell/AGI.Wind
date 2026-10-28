/** Display identity only; QQ nickname/card never grants application permissions. */
export function messageIdentity(role, group, sender, persona) {
  const assistant = role === 'assistant';
  if (group && !assistant) {
    const qq = String(sender?.external_user_id || '');
    return { side: 'left', name: sender?.card_name || sender?.display_name || qq || '群成员',
      assetId: null, avatarUrl: /^\d{5,20}$/.test(qq)
        ? 'https://q.qlogo.cn/headimg_dl?dst_uin=' + qq + '&spec=100' : '' };
  }
  return { side: group ? 'right' : (assistant ? 'left' : 'right'),
    name: assistant ? (persona.name || 'Nagi') : (persona.userName || '我'),
    assetId: assistant ? persona.avatarAssetId : persona.userAvatarAssetId, avatarUrl: '' };
}
