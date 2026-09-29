import unittest
import pandas as pd
from collector_risk.model import make_features, add_enhanced_features

class TemporalTests(unittest.TestCase):
    """Проверяет, что признаки и метки соблюдают порядок времени."""
    def test_future_alarms_do_not_change_past_features(self):
        """Проверяет отсутствие влияния будущей тревоги на прошлые признаки."""
        d=pd.DataFrame({'date':['2026-01-01','2026-01-02','2026-01-03'],
          'object_id':[1]*3,'category':['fire']*3,'alarms':[0,0,0],
          'readings':[10]*3,'channels':[2]*3,'numeric_mean':[1]*3,'numeric_max':[2]*3})
        before=add_enhanced_features(make_features(d))
        d.loc[2,'alarms']=10
        after=add_enhanced_features(make_features(d))
        for key in ['alarms_3d','alarms_7d','object_alarms','alarm_days_7obs']:
            self.assertEqual(before.loc[1,key],after.loc[1,key])

    def test_label_requires_adjacent_observed_day_and_prior_quiet_day(self):
        """Проверяет метку только для соседних наблюдаемых суток после спокойного дня."""
        d=pd.DataFrame({'date':['2026-01-01','2026-01-02','2026-01-04','2026-01-05'],
          'object_id':[1]*4,'category':['fire']*4,'alarms':[0,1,0,0],
          'readings':[10]*4,'channels':[2]*4,'numeric_mean':[1]*4,'numeric_max':[2]*4})
        f=make_features(d)
        self.assertTrue(f.loc[0,'eligible'])
        self.assertEqual(f.loc[0,'target'],1)
        self.assertFalse(f.loc[1,'eligible'])
        self.assertTrue(f.loc[2,'eligible'])
        self.assertEqual(f.loc[3,'alarms_3d'],0)  # Jan 2 is outside the 3-day window on Jan 5

    def test_other_system_signal_is_same_day_and_excludes_self(self):
        """Проверяет контекст соседней системы и исключение собственной тревоги."""
        d=pd.DataFrame({'date':['2026-01-01']*2+['2026-01-02']*2,
          'object_id':[1]*4,'category':['fire','power']*2,
          'alarms':[0,0,0,3],'readings':[5]*4,'channels':[1]*4,
          'numeric_mean':[1]*4,'numeric_max':[2]*4})
        f=add_enhanced_features(make_features(d))
        fire=f[(f.category=='fire')&(f.date==pd.Timestamp('2026-01-02'))].iloc[0]
        power=f[(f.category=='power')&(f.date==pd.Timestamp('2026-01-02'))].iloc[0]
        self.assertEqual(fire.other_power_alarms,3)
        self.assertEqual(power.other_power_alarms,0)
        self.assertEqual(fire.object_alarms,3)

if __name__=='__main__':unittest.main()
