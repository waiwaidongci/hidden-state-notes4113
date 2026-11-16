from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from src.config.database import get_db
from src.controllers.dependencies import current_user,writers
from src.schemas.filter import SessionInput,AppendInput,CorrectionInput
from src.services.filter_sessions import create_session,append_tokens,correct_positions,sessions,session_detail,request_compute
router=APIRouter(prefix='/filter-sessions',tags=['filter-sessions'])

@router.get('')
def index(db:Session=Depends(get_db),user=Depends(current_user)):return sessions(db)

@router.post('')
def create(data:SessionInput,db:Session=Depends(get_db),user=Depends(writers)):return create_session(db,data,user.email)

@router.get('/{id}')
def one(id:int,db:Session=Depends(get_db),user=Depends(current_user)):return session_detail(db,id)

@router.post('/{id}/append')
def append(id:int,data:AppendInput,db:Session=Depends(get_db),user=Depends(writers)):return append_tokens(db,id,data,user.email)

@router.post('/{id}/corrections')
def correct(id:int,data:CorrectionInput,db:Session=Depends(get_db),user=Depends(writers)):return correct_positions(db,id,data,user.email)

@router.post('/{id}/compute')
def recompute(id:int,force:bool=False,db:Session=Depends(get_db),user=Depends(writers)):return request_compute(db,id,user.email,force)
