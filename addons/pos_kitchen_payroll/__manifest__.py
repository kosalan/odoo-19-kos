{
    'name': 'POS Kitchen Payroll',
    'version': '19.0.1.0.0',
    'author': 'Kosalan Balarajah',
    'category': 'Human Resources',
    'summary': 'Hourly wage on employee contracts (used for hospitality payroll)',
    'description': """
        Adds an Hourly Wage field on the employee contract / version, and
        shows the implied monthly equivalent on the Payroll tab. Restaurant
        employees are typically paid hourly, not monthly.
    """,
    'depends': ['hr'],
    'data': [
        'views/hr_version_views.xml',
        'views/hr_employee_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
