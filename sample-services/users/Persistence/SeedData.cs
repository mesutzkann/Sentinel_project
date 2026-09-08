using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Users.Domain;

namespace Sentinel.Samples.Users.Persistence;

/// <summary>
/// Seeds a handful of customers so the checkout flow has something to run against on a fresh
/// database. Ids are fixed rather than random: the agent evaluation fixtures reference them, and
/// a re-seeded stack has to produce the same incidents.
/// </summary>
internal static class SeedData
{
    private static readonly User[] Users =
    [
        new()
        {
            Id = Guid.Parse("11111111-1111-1111-1111-111111111111"),
            Email = "ayse.demir@example.com",
            FullName = "Ayse Demir",
        },
        new()
        {
            Id = Guid.Parse("22222222-2222-2222-2222-222222222222"),
            Email = "mehmet.kaya@example.com",
            FullName = "Mehmet Kaya",
        },
        new()
        {
            Id = Guid.Parse("33333333-3333-3333-3333-333333333333"),
            Email = "elif.yilmaz@example.com",
            FullName = "Elif Yilmaz",
        },
    ];

    public static async Task EnsureSeededAsync(WebApplication app)
    {
        await using var scope = app.Services.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<UsersDbContext>();

        var seedIds = Users.Select(u => u.Id).ToArray();
        var existing = await db.Users
            .Where(u => seedIds.Contains(u.Id))
            .Select(u => u.Id)
            .ToListAsync();

        var missing = Users.Where(u => !existing.Contains(u.Id)).ToArray();
        if (missing.Length == 0)
        {
            return;
        }

        db.Users.AddRange(missing);
        await db.SaveChangesAsync();
    }
}
