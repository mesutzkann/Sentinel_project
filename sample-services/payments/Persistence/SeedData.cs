using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Payments.Domain;

namespace Sentinel.Samples.Payments.Persistence;

/// <summary>
/// Seeds the merchant balance rows that chaos scenario 3 (DB_DEADLOCK) contends over.
/// Fixed ids, so evaluation runs see the same rows every time.
/// </summary>
internal static class SeedData
{
    private static readonly MerchantBalance[] Balances =
    [
        new()
        {
            Id = Guid.Parse("aaaaaaaa-0000-0000-0000-000000000001"),
            MerchantCode = "MERCHANT-A",
            Balance = 0m,
        },
        new()
        {
            Id = Guid.Parse("aaaaaaaa-0000-0000-0000-000000000002"),
            MerchantCode = "MERCHANT-B",
            Balance = 0m,
        },
    ];

    public static async Task EnsureSeededAsync(WebApplication app)
    {
        await using var scope = app.Services.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<PaymentsDbContext>();

        var seedIds = Balances.Select(b => b.Id).ToArray();
        var existing = await db.MerchantBalances
            .Where(b => seedIds.Contains(b.Id))
            .Select(b => b.Id)
            .ToListAsync();

        var missing = Balances.Where(b => !existing.Contains(b.Id)).ToArray();
        if (missing.Length == 0)
        {
            return;
        }

        db.MerchantBalances.AddRange(missing);
        await db.SaveChangesAsync();
    }
}
