"""Public setup instructions; opening the exchange never includes account data."""

LIGHTER_API_KEYS_URL = 'https://robinhoodchain.lighter.xyz/apikeys'

LIGHTER_KEY_STEPS = (
    '1. Откройте «API Keys на сайте Lighter» кнопкой выше. Это Lighter именно на Robinhood Chain.\n'
    '2. Нажмите Connect Wallet → Robinhood Wallet. Подтвердите подключение на телефоне; '
    'для QR-кода используйте сканер Robinhood Wallet. Сверьте адрес с полем аккаунта ниже.\n'
    '3. Нажмите Generate API Key. Выберите свободный API Key Index: 4–156 или 158–254. '
    'Maker Only оставьте выключенным: бот использует IOC-ордера. Подтвердите привязку в кошельке.\n'
    '4. После успешной регистрации скопируйте Your Account Index, API Key Index и Private Key. '
    'Private Key показывается только при создании — скопируйте его до закрытия окна.\n'
    '5. Вставьте эти данные ниже, нажмите «Сохранить ключ», затем «Проверить доступ» в боте.'
)

LIGHTER_POINTS_NOTE = (
    'Robinhood описывает 2× для торговли через Robinhood Wallet. '
    'Множитель 2× для сделок бота через API пока не подтверждён.'
)
