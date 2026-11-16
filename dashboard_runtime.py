"""Process-shared dashboard scanner and paper writer, independent of tab widgets."""
from types import SimpleNamespace
from threading import RLock
from queue import SimpleQueue, Empty
import time
import pandas as pd
import streamlit as st
from paper_engine import PaperStore, refresh_cycle, worker_pool
from worker_store import cloud_journal
from scan_clock import scheduled_slot, regular_day, write_window

@st.cache_resource
def runtimes():
    return {}, RLock()

def get_runtime(key, mode, token, gist, owner):
    items, lock = runtimes()
    with lock:
        if key not in items:
            items[key]=Runtime(mode,token,gist,owner)
        return items[key]

class Runtime:
    def __init__(self, mode, github, gist, owner):
        self.lock=RLock()
        self.mode=mode
        self.github=github
        self.owner=owner
        self.store=PaperStore(github,gist)
        self.journal=pd.DataFrame()
        self.rows=[]
        self.candles={}
        self.notes=[]
        self.stamp=''
        self.slot=None
        self.requested_slot=None
        self.auto_slot=None
        self.progress=None
        self.queue=SimpleQueue()
        self.future=None
        self.load_future=None
        self.cloud=None
        self.loaded=False
        self.dirty=False
        self.error=''
        self.checked=-1e6
        self.cloud_status={}

    def load(self, previous_id):
        if self.mode:
            if self.cloud is None:
                self.cloud=cloud_journal(self.github,self.store.gist,'intraday')
            return self.cloud.read(snapshot=True,previous_id=previous_id)
        return self.store.load(),None,None

    def finish_load(self):
        try:
            frame,status,snap=self.load_future.result()
            if frame is not None:self.journal=frame
            if self.mode and status is not None:
                if snap is not None:
                    self.rows,self.candles,slot=snap
                    self.slot=pd.Timestamp(slot) if slot else None
                self.cloud_status=status
                self.notes=status.get('notes',[])
                self.stamp=status.get('checked_at','')
            self.loaded=True
            self.error=''
        except Exception as e:
            self.error=f'Paper journal / worker data unavailable: {str(e)[:180]}'
        finally:
            self.load_future=None

    def step(self, clock, settings, request, lookup, token, client, params, owner):
        with self.lock:
            # The first active owner controls both settings and the local single writer.
            # Another browser still receives the current snapshot and journal.
            if time.monotonic()-getattr(self,'owner_seen',0)>90:self.owner=owner
            controller=self.owner==owner
            if controller:self.owner_seen=time.monotonic()
            if self.load_future is not None and self.load_future.done():self.finish_load()
            if self.load_future is None and self.future is None and (self.mode or not self.loaded) and (request or time.monotonic()-self.checked>=15):
                self.checked=time.monotonic()
                self.load_future=worker_pool().submit(self.load,self.cloud_status.get('snapshot_id',''))
            while True:
                try:self.progress=self.queue.get_nowait()
                except Empty:break
            if self.future is not None and self.future.done():
                try:
                    rows,candles,journal,self.notes,_,self.dirty,self.stamp=self.future.result()
                    if rows or not self.rows:
                        self.rows,self.candles,self.slot=rows,candles,self.requested_slot
                    else:
                        self.notes.insert(0,'No new rows published. Showing the previous snapshot.')
                    if self.loaded:self.journal=journal
                except Exception as e:
                    self.notes=['Refresh failed: '+str(e)[:180]]
                self.future=None
            slot=scheduled_slot(clock,settings)
            if request and not self.mode and controller:self.manual_requested=True
            if not self.mode and controller and self.future is None and self.load_future is None:
                if (slot is not None and slot!=self.auto_slot) or getattr(self,'manual_requested',False):
                    self.manual_requested=False
                    historical=slot is None and (not regular_day(clock,settings) or not write_window(clock))
                    self.requested_slot=slot
                    if slot is not None:self.auto_slot=slot
                    self.progress=(0,0,'','')
                    self.queue=SimpleQueue()
                    self.policy=params.copy()
                    allowed=slot is not None and not historical
                    self.future=worker_pool().submit(refresh_cycle,token,client,lookup,list(lookup),self.journal,
                        **params,store=self.store,dirty=self.dirty,progress=self.queue,journal_ready=self.loaded,
                        slot=slot,allow_trades=allowed)
            return SimpleNamespace(rows=self.rows,candles=self.candles,journal=self.journal,notes=self.notes,
                stamp=self.stamp,slot=self.slot,progress=self.progress,future=self.future,error=self.error,
                status=self.cloud_status,gist=self.store.gist,controller=controller,
                policy=self.cloud_status.get('policy',{}) if self.mode else getattr(self,'policy',params))
