import contextlib
import io
import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import requests
import main


def response(body):
    result = Mock()
    result.json.return_value = body
    return result


class GreetingTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "APP_ID": "test-app", "APP_SECRET": "secret-never-log",
            "TEMPLATE_ID": "test-template", "USER_ID": " user-a\r\n\r\nuser-b\nuser-a ",
            "START_DATE": "2022-08-09", "BIRTHDAY": "10-11", "CITY": "北京",
        }
        self.config = main.load_config(self.env)
        self.session = Mock()

    def test_missing_configuration_names_are_reported_without_values(self):
        with self.assertRaisesRegex(ValueError, "APP_ID.*APP_SECRET.*TEMPLATE_ID.*USER_ID"):
            main.load_config({key: self.env[key] for key in ["CITY", "START_DATE", "BIRTHDAY"]})

    def test_blank_and_duplicate_recipients_are_removed(self):
        self.assertEqual(self.config.user_ids, ("user-a", "user-b"))

    def test_invalid_dates_are_reported(self):
        for field, value in [("START_DATE", "2022-02-30"), ("BIRTHDAY", "02-30")]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                main.load_config(dict(self.env, **{field: value}))

    def test_birthday_is_zero_on_the_day_and_rolls_over_afterwards(self):
        self.assertEqual(main.get_birthday((10, 11), date(2026, 10, 11)), 0)
        self.assertEqual(main.get_birthday((10, 11), date(2026, 10, 12)), 364)

    def test_full_birth_date_uses_month_and_day_for_annual_countdown(self):
        config = main.load_config(dict(self.env, BIRTHDAY=" 1998-10-11 "))
        self.assertEqual(config.birthday, (10, 11))
        self.assertEqual(main.get_birthday(config.birthday, date(2026, 10, 11)), 0)
        self.assertEqual(main.get_birthday(config.birthday, date(2026, 10, 12)), 364)

    def test_full_birth_date_validates_the_provided_year(self):
        config = main.load_config(dict(self.env, BIRTHDAY="2000-02-29"))
        self.assertEqual(config.birthday, (2, 29))
        for value in ["2001-02-29", "1998-02-30", "19981011", "0000-10-11"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                main.load_config(dict(self.env, BIRTHDAY=value))

    def test_leap_day_birthday(self):
        expected = (date(2028, 2, 29) - date(2026, 10, 11)).days
        self.assertEqual(main.get_birthday((2, 29), date(2026, 10, 11)), expected)
        self.assertEqual(main.get_birthday((2, 29), date(2028, 2, 29)), 0)

    def test_city_suffix_fallback_avoids_same_named_village(self):
        self.session.get.side_effect = [
            response({"results": [{"name": "承德", "country_code": "CN", "latitude": 43, "longitude": 126}]}),
            response({"results": [{"name": "承德市", "country_code": "CN", "population": 449325,
                                   "latitude": 40.95, "longitude": 117.96}]}),
        ]
        self.assertEqual(main.get_coordinates(self.session, "承德"), (40.95, 117.96))
        self.assertEqual(self.session.get.call_args.kwargs["params"]["name"], "承德市")

    def test_coordinate_override_and_validation(self):
        self.assertEqual(main.get_coordinates(self.session, "北京", "39.9,116.4"), (39.9, 116.4))
        self.session.get.assert_not_called()
        for value in ["91,116", "39,181", "nan,116", "39", "a,b"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                main.get_coordinates(self.session, "北京", value)

    def test_weather_response_maps_codes_and_temperatures(self):
        self.session.get.return_value = response({
            "current": {"weather_code": 61, "temperature_2m": 12.9},
            "daily": {"temperature_2m_max": [19.7], "temperature_2m_min": [-1.1]},
        })
        self.assertEqual(main.get_weather(self.session, "北京", "39.9,116.4"), ("小雨", 12, 19, -2))
        self.assertEqual(self.session.get.call_args.kwargs["params"]["timezone"], "Asia/Shanghai")

    def test_weather_failure_never_blocks_content(self):
        for body in [[], {"code": 1014}, {"current": None, "daily": {}}, {"current": {}, "daily": {}}]:
            self.session.get.return_value = response(body)
            with self.subTest(body=body):
                self.assertEqual(main.get_weather(self.session, "北京", "39.9,116.4"), main.WEATHER_FALLBACK)
        self.session.get.side_effect = requests.Timeout("weather timeout")
        self.assertEqual(main.get_weather(self.session, "北京"), main.WEATHER_FALLBACK)

    def test_words_failure_has_bounded_fallback(self):
        self.session.get.side_effect = requests.Timeout("words timeout")
        self.assertEqual(main.get_words(self.session), main.WORDS_FALLBACK)
        self.assertEqual(self.session.get.call_count, 1)

    def test_empty_or_malformed_words_use_fallback(self):
        for body in [{}, {"data": {"text": " "}}, {"data": {"text": None}}]:
            self.session.get.return_value = response(body)
            with self.subTest(body=body):
                self.assertEqual(main.get_words(self.session), main.WORDS_FALLBACK)

    def test_date_and_original_template_fields_survive_api_failures(self):
        self.session.get.side_effect = requests.Timeout("offline")
        data = main.build_data(self.config, self.session, today=date(2026, 10, 11))
        self.assertEqual(set(data), {"date", "weather", "temperature", "highest", "lowest",
                                    "love_days", "birthday_left", "words"})
        self.assertEqual(data["birthday_left"]["value"], "0")
        self.assertEqual(data["love_days"]["value"], str((date(2026, 10, 11) - date(2022, 8, 9)).days))
        self.assertTrue(all(isinstance(item["value"], str) for item in data.values()))

    def test_beijing_date_is_used_when_utc_is_previous_day(self):
        frozen = Mock()
        frozen.now.side_effect = lambda tz: datetime(2026, 10, 10, 16, 1, tzinfo=timezone.utc).astimezone(tz)
        with patch.object(main, "datetime", frozen), patch.object(main, "get_weather", return_value=main.WEATHER_FALLBACK), patch.object(main, "get_words", return_value=main.WORDS_FALLBACK):
            data = main.build_data(self.config, self.session)
        self.assertEqual(data["date"]["value"], "2026-10-11")
        self.assertEqual(data["birthday_left"]["value"], "0")

    def test_wechat_errors_are_not_counted_as_success_and_other_users_continue(self):
        self.session.get.return_value = response({"access_token": "test-token"})
        self.session.post.side_effect = [response({"errcode": 40003}), response({"errcode": 0, "msgid": 1})]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.send_messages(self.session, self.config, {}), 1)
        self.assertIn("成功发送 1 条消息，失败 1 条", output.getvalue())
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["touser"], "user-b")

    def test_successful_sends_preserve_template_payload(self):
        self.session.get.return_value = response({"access_token": "test-token"})
        self.session.post.return_value = response({"errcode": 0})
        data = {"weather": {"value": "晴", "color": "#123456"}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.send_messages(self.session, self.config, data), 0)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["template_id"], "test-template")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["data"], data)

    def test_token_error_is_explicit_and_does_not_send(self):
        self.session.get.return_value = response({"errcode": 40164})
        with self.assertRaisesRegex(ValueError, "40164"):
            main.send_messages(self.session, self.config, {})
        self.session.post.assert_not_called()

    def test_transport_errors_do_not_leak_credentials(self):
        self.session.get.return_value = response({"access_token": "test-token"})
        self.session.post.side_effect = requests.HTTPError("https://example.invalid?access_token=secret-never-log")
        with self.assertLogs(main.LOGGER, level="ERROR") as logs, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.send_messages(self.session, self.config, {}), 1)
        self.assertNotIn("secret-never-log", "\n".join(logs.output))

    def test_dry_run_never_calls_wechat_even_with_credentials_present(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(main, "create_session") as factory, patch.object(main, "build_data", return_value={}), patch.object(main, "send_messages") as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--dry-run"]), 0)
        send.assert_not_called()
        factory.return_value.__enter__.return_value.post.assert_not_called()

    def test_retry_policy_excludes_post(self):
        with main.create_session() as session:
            retry = session.adapters["https://"].max_retries
            self.assertTrue(retry.is_retry("GET", 503))
            self.assertFalse(retry.is_retry("POST", 503))

    def test_check_mode_validates_wechat_without_sending_or_logging_content(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(main, "create_session"), patch.object(main, "build_data", return_value={"words": "private"}), patch.object(main, "get_access_token", return_value="secret-never-log") as token, patch.object(main, "send_messages") as send, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.main(["--check"]), 0)
        token.assert_called_once()
        send.assert_not_called()
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("secret-never-log", output.getvalue())

    def test_malformed_send_response_does_not_block_next_user(self):
        self.session.get.return_value = response({"access_token": "test-token"})
        self.session.post.side_effect = [response([]), response({"errcode": 0})]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.send_messages(self.session, self.config, {}), 1)
        self.assertEqual(self.session.post.call_count, 2)

    def test_restricted_wechat_account_has_specific_guidance_without_raw_error(self):
        self.session.get.return_value = response({"errcode": 50002, "errmsg": "user limited rid: secret-never-log"})
        with self.assertRaisesRegex(ValueError, "50002.*user limited.*账号状态") as error:
            main.get_access_token(self.session, self.config)
        self.assertNotIn("secret-never-log", str(error.exception))
        self.session.post.assert_not_called()

    def test_template_inspection_checks_only_the_configured_template(self):
        self.session.get.return_value = response({"template_list": [
            {"template_id": "other-template", "content": "{{words.DATA}}"},
            {"template_id": "test-template", "content": "{{random.DATA}}"},
        ]})
        self.assertFalse(main.inspect_template(self.session, self.config, "test-token"))
        self.session.get.return_value = response({"template_list": [
            {"template_id": "test-template", "content": "今日文案：{{ words.DATA }}"},
        ]})
        self.assertTrue(main.inspect_template(self.session, self.config, "test-token"))

    def test_unsupported_template_list_does_not_break_authentication_check(self):
        self.session.get.return_value = response({"errcode": 48001})
        self.assertIsNone(main.inspect_template(self.session, self.config, "test-token"))


if __name__ == "__main__":
    unittest.main()
