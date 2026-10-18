"""Authenticated archive API. This process does not submit trades."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os, json, sqlite3, re
from fastapi import HTTPException, Query
from archive import Archive
from live_api import create_app, ReadArchive, utc, utcnow, MAX_CURSOR
from interpretation import interpret_signal, PARSER_VERSION

CHAT_ID=-1004384036660

def create_cloud_app(db_path, password_record, chat_id=CHAT_ID):
    status_file='service-status.json' if os.environ.get('FLY_PAPER_EXECUTION')=='1' else 'feed-reader-status.json'
    app=create_app(db_path,password_record,chat_id,status_path=Path(db_path).parent/status_file)
    source=ReadArchive(db_path,chat_id)
    def items(day=None, message_id=None, after=0, limit=100, signals_only=False):
        sql='SELECT rowid AS cursor, * FROM messages WHERE chat_id=? AND rowid>?'; args=[chat_id,after]
        if day:
            start=datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo('America/New_York'))
            end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=ZoneInfo('America/New_York'))
            sql+=' AND sent_at>=? AND sent_at<?'; args.extend([start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat()])
        if message_id is not None: sql+=' AND message_id=?'; args.append(message_id)
        sql+=' ORDER BY rowid'
        with source.connection() as db:
            rows=db.execute(sql,args)
            result=[]
            for r in rows:
                signal=interpret_signal(r['text'],r['sent_at'])
                if signals_only and signal['kind']=='ignore': continue
                result.append(dict(cursor=r['cursor'], messageId=r['message_id'],revision=r['revision'],sentAt=utc(r['sent_at']),editedAt=utc(r['edited_at']),capturedAt=utc(r['captured_at']),text=r['text'],replyToMessageId=r['reply_to'],mediaType=r['media_type'],isHistorical=bool(r['historical']),isEdited=bool(r['edited_at']),signal=signal))
                if len(result)>limit: break
        more=len(result)>limit; result=result[:limit]
        return dict(parserVersion=PARSER_VERSION,items=result,nextCursor=result[-1]['cursor'] if result else after,hasMore=more,generatedAt=utcnow())
    @app.get('/api/v1/callouts')
    def callouts(date: date | None = None, after: int=Query(0,ge=0,le=MAX_CURSOR),limit: int=Query(100,ge=1,le=500)):
        return items(date,after=after,limit=limit)
    @app.get('/api/v1/signals')
    def signals(date: date | None = None,after: int=Query(0,ge=0,le=MAX_CURSOR),limit: int=Query(100,ge=1,le=500)):
        return items(date,after=after,limit=limit,signals_only=True)
    @app.get('/api/v1/execution')
    def execution_status():
        path=Path(db_path).parent/'service-status.json'
        try:
            if path.stat().st_size>1000000: raise ValueError('Oversized status')
            value=json.loads(path.read_text())
            return {k:value.get(k) for k in ('status','updated_at','environment','strategy_profile','cursor','orders_by_status','exit_watch','exit_reviews','telegram','error','parser_version')}
        except (OSError, ValueError, TypeError):
            return {'state':'not_configured'}
    @app.get('/api/v1/messages/{message_id}')
    def message(message_id: int,after: int=Query(0,ge=0,le=MAX_CURSOR),limit: int=Query(100,ge=1,le=500)):
        result=items(message_id=message_id,after=after,limit=limit)
        if not result['items'] and after==0: raise HTTPException(404)
        return result
    return app

def main():
    import uvicorn
    data=Path(os.environ.get('DATA_DIR','/data')); data.mkdir(parents=True,exist_ok=True)
    db=data/'messages.sqlite3'; archive=Archive(db); archive.close()
    record=json.loads(os.environ['FEED_AUTH_RECORD'])
    app=create_cloud_app(db,record)
    uvicorn.run(app,host='0.0.0.0',port=8080,workers=1,proxy_headers=False,access_log=False)

if __name__=='__main__': main()
