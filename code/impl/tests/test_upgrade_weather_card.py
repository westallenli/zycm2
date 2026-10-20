"""Weather display uses audited, event-bound observations without inventing data."""
import unittest
from dataclasses import replace
from panda_brain.sand_command import weather_snapshot
from tests import test_upgrade_source_review as fixture

class WeatherCardTests(unittest.TestCase):
    setUp=fixture.SourceReviewTests.setUp
    ingest=fixture.SourceReviewTests.ingest

    def test_no_observations_are_not_reported_as_fine_weather_or_zero(self):
        before=self.ws.version
        value=weather_snapshot(self.ws,fixture.EVENT)
        self.assertFalse(value['available']);self.assertEqual({},value['readings'])
        self.assertEqual('气象资料未接入',value['notice']);self.assertEqual(before,self.ws.version)

    def test_zero_wind_is_preserved_with_original_units_time_and_simulation_label(self):
        self.ingest(value=0)
        before=self.ws.version;value=weather_snapshot(self.ws,fixture.EVENT)
        row=value['readings']['wind_speed_m_s']
        self.assertEqual(0,row['value']);self.assertEqual('m/s',row['unit'])
        self.assertEqual('2026-10-07T10:00:00+08:00',row['collected_at'])
        self.assertIn('模拟',value['notice']);self.assertEqual(before,self.ws.version)

    def test_latest_station_does_not_mix_other_station_temperature(self):
        self.ingest(hour=10,metric='temperature_c',value=20,unit='C')
        self.ingest(hour=11,site_id='second-site',value=4)
        value=weather_snapshot(self.ws,fixture.EVENT)
        self.assertEqual('second-site',value['station'])
        self.assertNotIn('temperature_c',value['readings'])
        self.assertEqual(4,value['readings']['wind_speed_m_s']['value'])

    def test_same_station_displays_existing_temperature_and_humidity_only(self):
        self.ingest(metric='temperature_c',value=21,unit='C')
        self.ingest(metric='humidity_pct',value=58,unit='%')
        value=weather_snapshot(self.ws,fixture.EVENT)
        self.assertEqual({'temperature_c','humidity_pct'},set(value['readings']))
        self.assertNotIn('wind_direction_deg',value['readings'])

    def test_late_reading_does_not_replace_current_wind(self):
        self.ingest(hour=11,value=5);self.ingest(hour=9,value=1)
        self.assertEqual(5,weather_snapshot(self.ws,fixture.EVENT)['readings']['wind_speed_m_s']['value'])

    def test_foreign_event_rejected_and_unaudited_claim_not_displayed(self):
        self.ingest()
        with self.assertRaises(ValueError):weather_snapshot(self.ws,'other-event')
        original=self.ws.claims[fixture.SUBJECT]
        self.ws.claims[fixture.SUBJECT]=replace(original,value={'value':99,'unit':'m/s'})
        self.assertFalse(weather_snapshot(self.ws,fixture.EVENT)['available'])
