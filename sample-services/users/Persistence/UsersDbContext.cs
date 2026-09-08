using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Users.Domain;

namespace Sentinel.Samples.Users.Persistence;

public sealed class UsersDbContext : DbContext
{
    public const string Schema = "svc_users";

    public UsersDbContext(DbContextOptions<UsersDbContext> options) : base(options)
    {
    }

    public DbSet<User> Users => Set<User>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.HasDefaultSchema(Schema);

        modelBuilder.Entity<User>(entity =>
        {
            entity.ToTable("users");
            entity.HasKey(u => u.Id);

            entity.Property(u => u.Email).HasMaxLength(256).IsRequired();
            entity.Property(u => u.FullName).HasMaxLength(200).IsRequired();

            entity.HasIndex(u => u.Email).IsUnique();
        });
    }
}
