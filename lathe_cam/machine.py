from dataclasses import dataclass,field,asdict
import json

# KMT-KE with SINUMERIK 808D. The site and the passport were not available when this file was
# written, so every limit below is a typical value for this class of lathe: check them against
# the machine passport before the first real program (see README).
@dataclass
class Machine:
    name:str='KMT-KE'
    control:str='SINUMERIK 808D ADVANCED T'
    max_diameter:float=400.     # swing over bed, mm (ПРОВЕРИТЬ)
    max_length:float=1000.      # distance between centres, mm (ПРОВЕРИТЬ)
    x_travel:float=240.         # X stroke, mm in radius (ПРОВЕРИТЬ)
    z_travel:float=950.         # Z stroke, mm (ПРОВЕРИТЬ)
    max_rpm:int=2000            # spindle, rpm (ПРОВЕРИТЬ)
    max_feed:float=5000.        # mm/min (ПРОВЕРИТЬ)
    tool_positions:int=4        # turret/tool post stations (ПРОВЕРИТЬ)
    spindle_bore:float=52.      # mm (ПРОВЕРИТЬ)
    # Rear tool post turns with M3, front with M4; the cutting edge must face the material.
    spindle_cw:str='M3'
    coolant_on:str='M8'
    coolant_off:str='M9'
    work_offset:str='G54'
    tool_change_x:float=200.    # tool change point, diameter
    tool_change_z:float=150.
    line_numbers:bool=False
    line_step:int=10

    def check_rpm(self,rpm):
        if not 0<rpm<=self.max_rpm:raise ValueError(f'Обороты {rpm} вне диапазона 1..{self.max_rpm}')
    def check_tool(self,number):
        if not 1<=number<=self.tool_positions:raise ValueError(f'Позиция инструмента T{number} вне 1..{self.tool_positions}')

def load(path=None,overrides=None):
    data={}
    if path:
        with open(path,encoding='utf-8') as f:data=json.load(f)
    data.update(overrides or {})
    known=set(asdict(Machine()))
    unknown=set(data)-known
    if unknown:raise ValueError('Неизвестные параметры станка: '+', '.join(sorted(unknown)))
    return Machine(**data)
