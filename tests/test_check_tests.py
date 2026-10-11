import unittest

import check_tests


class ScrubTest(unittest.TestCase):
    def test_secret_values_and_addresses_never_reach_the_output(self):
        env = {"MAILRU_APP_PASSWORD_2": "Pass-Word-123456", "LEADS_TOKEN": "tok-7777777", "CHECKO_API_KEY": "key12345",
               "PORT": "8000", "SHORT_PASSWORD": "abc"}
        secrets = check_tests.secret_values(env)
        self.assertEqual(sorted(secrets), sorted(["Pass-Word-123456", "tok-7777777", "key12345"]))   # PORT и короткое значение - не секреты
        text = "AssertionError: 'Pass-Word-123456' != 'x'; send to Ivan.Petrov@mail.ru with tok-7777777 and key12345"
        shown = check_tests.scrub(text, secrets)
        for secret in ("Pass-Word-123456", "tok-7777777", "key12345", "Ivan.Petrov@mail.ru"):
            self.assertNotIn(secret, shown)
        self.assertIn("I***@mail.ru", shown)

    def test_last_lines_keep_only_the_error_and_are_short(self):
        trace = "Traceback (most recent call last):\n  File \"x.py\", line 1, in f\n    boom()\nAssertionError: 1 != 2\n"
        self.assertEqual(check_tests.last_lines(trace), "boom() | AssertionError: 1 != 2")
        self.assertLessEqual(len(check_tests.last_lines("x" * 5000)), check_tests.MAX_LINE + 3)


class RunSuiteTest(unittest.TestCase):
    def test_failures_and_errors_are_listed_by_name_and_the_tests_stay_silent(self):
        class Sample(unittest.TestCase):
            def test_ok(self):
                print("шум теста")

            def test_bad(self):
                self.assertEqual(1, 2)

            def test_boom(self):
                raise RuntimeError("взрыв")

        ran, problems = check_tests.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Sample))
        self.assertEqual(ran, 3)
        self.assertEqual(sorted((kind, name.split()[0]) for kind, name, _ in problems),
                         [("ERROR", "test_boom"), ("FAIL", "test_bad")])
        self.assertTrue(any("AssertionError: 1 != 2" in message for _, _, message in problems))
        self.assertTrue(any("RuntimeError: взрыв" in message for _, _, message in problems))


if __name__ == "__main__":
    unittest.main()
