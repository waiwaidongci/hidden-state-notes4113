from pydantic import BaseModel,Field,ConfigDict,model_validator

class SessionInput(BaseModel):
 model_config=ConfigDict(str_strip_whitespace=True)
 study_id:int=Field(gt=0,strict=True)
 profile_id:int=Field(gt=0,strict=True)
 name:str=Field(min_length=2,max_length=180)
 initial_tokens:list[str]=Field(default_factory=list,max_length=2000)

class AppendInput(BaseModel):
 tokens:list[str]=Field(min_length=1,max_length=200)
 expected_revision:int=Field(ge=0,strict=True)

class PositionCorrection(BaseModel):
 position:int=Field(ge=1,le=2000,strict=True)
 token:str=Field(min_length=1,max_length=80)

class CorrectionInput(BaseModel):
 expected_revision:int=Field(ge=0,strict=True)
 corrections:list[PositionCorrection]=Field(min_length=1,max_length=200)

 @model_validator(mode='after')
 def unique_positions(self):
  positions=[c.position for c in self.corrections]
  if len(positions)!=len(set(positions)):raise ValueError('同一位置只能更正一次')
  return self
