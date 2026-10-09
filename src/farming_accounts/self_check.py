"""Offline packaged-runtime check; no account registration or network writes."""

import asyncio
import ctypes


def signer_self_check():
    from eth_account import Account
    from eth_account.messages import encode_defunct
    owner = Account.create()
    message = encode_defunct(text='Hedge Bot offline runtime verification')
    signature = owner.sign_message(message).signature
    assert Account.recover_message(message, signature=signature) == owner.address

    async def check():
        import lighter
        private, public, error = lighter.create_api_key()
        assert error is None and len(private) == 82 and len(public) == 82
        client = lighter.SignerClient(url='https://api.rh.lighter.xyz', account_index=1,
                                     api_private_keys={4: private}, chain_id=466324)
        try:
            tx_type, payload, tx_hash, error = client.sign_create_order(
                market_index=24, client_order_index=12345, base_amount=10000, price=77700,
                is_ask=True, order_type=1, time_in_force=0, reduce_only=False,
                order_expiry=0, nonce=0, api_key_index=4)
            assert error is None and tx_type == 14 and payload and tx_hash
            native = client.signer.SignChangePubKey(ctypes.c_char_p(public.encode('utf-8')), 0, 0, 4, 1)
            decoded = {name: lighter.signer_client.decode_and_free(getattr(native, name))
                       for name in ('err', 'txInfo', 'txHash', 'messageToSign')}
            assert native.txType == 8 and decoded['err'] is None and all(
                decoded[name] for name in ('txInfo', 'txHash', 'messageToSign'))
        finally:
            await client.close()
    asyncio.run(check())
