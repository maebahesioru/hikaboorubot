# 通知APIテスト v3（本家twifork mainのget_notifications使用）
import asyncio, orjson
from twikit import Client

async def main():
    c = Client(language='ja')
    with open('/app/data/cookie.json', 'rb') as f:
        data = orjson.loads(f.read())
    if isinstance(data, dict) and 'auth_token' in data:
        cookies = {'auth_token': data['auth_token'], 'ct0': data.get('ct0', '')}
    elif isinstance(data, list):
        cookies = {d['name']: d['value'] for d in data}
    c.set_cookies(cookies)
    uid = await c.user_id()
    print('user_id:', uid)
    notifs = await c.get_notifications('Mentions', count=10)
    print('notifications count:', len(notifs))
    for n in notifs:
        t = n.tweet
        if t is None:
            continue
        is_quote = bool(getattr(t, 'is_quote_status', False))
        in_reply = str(getattr(t, 'in_reply_to', 0))
        qid = ''
        try:
            if is_quote:
                qid = str(t.quoted_status_id() or '')
        except Exception:
            pass
        uname = ''
        try:
            uname = t.user.screen_name
        except Exception:
            pass
        print(f'  tweet={t.id} user={uname} quote={is_quote} quote_target={qid} in_reply_to={in_reply}')
        print(f'    text: {str(getattr(t, "text", ""))[:60]}')

asyncio.run(main())
