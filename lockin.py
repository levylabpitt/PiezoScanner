'''
Levylab FLEX instrument driver for Levylab Multichannel-lockin
<https://github.com/levylabpitt/Multichannel-Lockin>

Authors: 
Pubudu Wijesinghe <pubudu.wijesinghe@levylab.org>

Contact an author for any queries.
'''

from flex.inst.base import Instrument
from flex.inst.levylab.insttypes.DAQ import DAQ
import time
import os
from datetime import datetime, timedelta
from flex.db import db_dataviewer as dv

_DEFAULT_ADDRESS = 'tcp://localhost:29170'
_LABVIEW_CLASS_NAME = "Instrument.Lockin.lvclass"

logpath = os.path.join(os.environ.get('LOCALAPPDATA'), 'Levylab', 'FLEX', 'logs')
os.makedirs(logpath, exist_ok=True)

class Lockin(Instrument, DAQ):
    def __init__(self, address=_DEFAULT_ADDRESS):
        super().__init__(address, log_file=os.path.join(logpath, "Lockin.log"))
          
    def getAO(self, channel):
        cmd = 'getAO'
        params = {'channel': channel}
        response = self._send_command(cmd, params)
        return response['result']
    
    def getAI(self, channel):
        cmd = 'getAI'
        params = {'channel': channel}
        response = self._send_command(cmd, params)
        return response['result']
    
    def setAO_Amplitude(self, channel: int, value: float) -> None:
        cmd = 'setAO_Amplitude'
        param = {'Channel': channel, 'Amplitude': value}
        self._send_command(cmd, param)

    def setAO_DC(self, channel: int, value: float) -> None:
        cmd = 'setAO_DC'
        param = {'Channel': channel, 'DC': value}
        self._send_command(cmd, param)

    def setAO_Frequency(self, channel: int, value: float) -> None:
        cmd = 'setAO_Frequency'
        param = {'Channel': channel, 'Frequency': value}
        self._send_command(cmd, param)

    def setAO_Phase(self, channel: int, value: float) -> None:
        cmd = 'setAO_Phase'
        param = {'Channel': channel, 'Phase': value}
        self._send_command(cmd, param)

    def setAO_Function(self, channel: int, value: str) -> None:
        """
        Set the function of the specified analog output (AO) channel.
        Parameters:
        channel (int): The AO channel number to set the function for.
        value (str): The function to set for the AO channel. Must be one of {"Sine", "Triangle", "Square"}.
        Raises:
        ValueError: If the provided value is not one of the allowed values.
        """

        allowed_values = {"Sine", "Triangle", "Square"}
        if value not in allowed_values:
            raise ValueError(f"Invalid value: {value}. Allowed values are: {', '.join(allowed_values)}")
        
        cmd = 'setAO_Function'
        param = {'Channel': channel, 'Function': value}
        self._send_command(cmd, param)

    def getResults(self) -> dict:
        cmd = 'getResults'
        return self._send_command(cmd)['result']
    
    def setState(self, value: str) -> None:
        cmd = 'setState'
        param = {"State": value}
        self._send_command(cmd, param)
    
    def getState(self) -> str:
        cmd = 'getState'
        return self._send_command(cmd)['result']
    
    def setSweepTime(self, value: float) -> None:
        cmd = 'setSweepTime'
        param = value
        self._send_command(cmd, param)
    
    def setSamplingMode(self, value: str) -> None:
        cmd = 'setSamplingFsMode'
        param = value
        self._send_command(cmd, param)
    
    def getSweepWaveforms(self) -> dict:
        cmd = 'getSweepWaveforms'
        return self._send_command(cmd)['result']

    def setSweep(self, sweep_config) -> None:
        '''
        sweep_config format:
        sweep_config = {"Sweep Time (s)":sweep_time,
                 "Initial Wait (s)":2,
                 "Return to Start":False,
                 "Channels":[{"Enable?":True,
                              "Channel":channel,
                              "Start":start,
                              "End":stop,
                              "Pattern": "Ramp /",
                              "Table":[]},
                              ]}
        '''
        self._send_command('setSweep', sweep_config)


# -------------- Custom functions ---------------->

    def get_lockin_result(self, channel: int, param: str, ref: int = 1) -> float:
        key = f"AI{channel}.{param}" if param == "Mean" else f"AI{channel}.Ref{ref}.{param}"
        response = self._send_command('getResults')
        results = response['result']['Results (Dictionary)']
        results_dict = {item['key']: item['value'] for item in results}
        return results_dict.get(key)

    def lockin_sweep(self, sweep_config: dict, timeout=10) -> None:        
        if self.getState() == 'sweeping':
            raise Exception('Request Denied! Already sweeping')    
        elif self.getState() == 'idle':
            self.setState('start')   
        self.setSweep(sweep_config)
        time.sleep(0.5)
        self.setState('start sweep')
        # wait for the sweep time since it'll anyway take that long (saves processor resources)
        wait_time = sweep_config.get("Sweep Time (s)") + sweep_config.get("Initial Wait (s)")
        time.sleep(wait_time) 
        start_time = time.time()
        while self.getState() == 'sweeping':
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Sweep operation timed out after {timeout} seconds. Please check the Multichannel Lock-in Application.")
            time.sleep(0.5)

    def set_backgate(self, bg_channel:int, bg_target:float, sweep_rate:float, initial_wait:float = 1, tol:float = 1e-3):
        """
        Sweep the backgate voltage to a target value.

        Parameters
        ----------
        bg_channel : int
            Analog output channel used for the backgate.

        bg_target : float
            Target backgate voltage in volts (V).

        sweep_rate : float
            Sweep rate in seconds per volt (s/V).

        initial_wait : float, optional
            Delay before starting the sweep in seconds. Default is 1 s.

        tol : float, optional
            Voltage tolerance in volts used to determine whether
            the gate is already at the target voltage.
            Default is 1e-3 V.
        """
                
        current_bg = self.getAO(bg_channel)[bg_channel-1]['Y'][0]

        # Skip sweep if already at target
        if abs(bg_target - current_bg) < tol:
            print(f"Backgate already at {bg_target:.2f} V. Target is within tolerance.")
            return

        duration = abs(bg_target - current_bg) * sweep_rate

        print(f"Sweeping backgate from {current_bg:.2f} to {bg_target:.2f} V...")

        sweep_config = {
            "Sweep Time (s)": duration,
            "Initial Wait (s)": initial_wait,
            "Return to Start": False,
            "Channels": [
                {
                    "Enable?": True,
                    "Channel": bg_channel,
                    "Start": current_bg,
                    "End": bg_target,
                    "Pattern": "Ramp /",
                    "Table": []
                },
            ]
        }

        self.lockin_sweep(sweep_config)

if __name__ == "__main__":
    # Test the MCLockin class
    lockin = Lockin("tcp://localhost:29170",)
    lockin.set_amplitude(1, 0.01)
    lockin.set_freq(1, 13)
    lockin.set_func(1, 'Sine')
    print(lockin.get_lockin(channel=1, param='Theta', ref=1))
    lockin.close()
