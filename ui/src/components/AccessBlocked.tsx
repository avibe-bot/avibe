import { useTranslation } from 'react-i18next';

import { Button } from './ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';

// ui/design: chrome/access-blocked. Shown when the server is up but refuses to open
// Avibe at this address: it names the two ways on instead of stranding the person
// in the setup wizard. An open page reaches it when the address it was loaded from
// stops opening Avibe, as a network address does once the Avibe is bound to an account.
export const AccessBlocked = ({ code }: { code: string | null }) => {
    const { t } = useTranslation();
    return (
        <main className="min-h-screen flex items-center justify-center bg-bg text-text p-4">
            <Card className="max-w-md w-full">
                <CardHeader>
                    <CardTitle>{t('accessBlocked.title')}</CardTitle>
                    <CardDescription>{t('accessBlocked.body')}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <p className="text-sm text-muted">{t('accessBlocked.hint')}</p>
                    {code ? (
                        <p className="text-xs text-muted font-mono">{t('accessBlocked.code', { code })}</p>
                    ) : null}
                    <Button onClick={() => window.location.reload()}>{t('accessBlocked.retry')}</Button>
                </CardContent>
            </Card>
        </main>
    );
};
